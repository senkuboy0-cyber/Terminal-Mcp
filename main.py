import asyncio
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import psutil
import requests
import uvicorn
from bs4 import BeautifulSoup
from ddgs import DDGS
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from mcp.server import Server
from mcp.server.sse import SseServerTransport
import mcp.types as types

# ─────────────────────────────────────────────────────────────────────────────
# Directories
# ─────────────────────────────────────────────────────────────────────────────
DOWNLOADS_DIR = "/app/downloads"
TEMP_DIR = "/app/temp"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Process tracking (for stream_output / get_process_output)
# ─────────────────────────────────────────────────────────────────────────────
process_buffer: dict[str, list[str]] = {}
process_status: dict[str, str] = {}
process_read_index: dict[str, int] = {}
active_processes: dict[str, subprocess.Popen] = {}

# ─────────────────────────────────────────────────────────────────────────────
# MCP server + SSE transport
# ─────────────────────────────────────────────────────────────────────────────
mcp_server = Server("ai-terminal")
sse_transport = SseServerTransport("/messages/")

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="AI Terminal MCP Server")


@app.get("/")
async def root():
    """Root endpoint - confirms MCP server is online."""
    return PlainTextResponse("mcp online")


@app.get("/download/{filename:path}")
async def download_endpoint(filename: str):
    """Serve a file from /app/downloads with automatic browser download trigger."""
    file_path = os.path.join(DOWNLOADS_DIR, filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(
            file_path,
            headers={
                "Content-Disposition": f'attachment; filename="{os.path.basename(filename)}"'
            },
        )
    return PlainTextResponse("File not found", status_code=404)


@app.get("/sse")
async def sse_endpoint(request: Request):
    """MCP SSE connection endpoint. AI clients connect here."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )


@app.post("/messages/")
async def messages_endpoint(request: Request):
    """MCP message posting endpoint."""
    await sse_transport.handle_post_message(
        request.scope, request.receive, request._send
    )


# ─────────────────────────────────────────────────────────────────────────────
# Background process runner helper
# ─────────────────────────────────────────────────────────────────────────────
def _run_background(pid: str, command: str) -> None:
    """Run a shell command in a background thread, capturing output line by line."""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        active_processes[pid] = proc
        for line in iter(proc.stdout.readline, ""):
            process_buffer[pid].append(line.rstrip("\n"))
        proc.wait()
        process_status[pid] = "finished" if proc.returncode == 0 else "error"
        active_processes.pop(pid, None)
    except Exception as exc:
        process_buffer.setdefault(pid, []).append(f"Exception: {exc}")
        process_status[pid] = "error"


# ─────────────────────────────────────────────────────────────────────────────
# MCP: list tools
# ─────────────────────────────────────────────────────────────────────────────
@mcp_server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

        # ── GROUP 1: Command Execution ────────────────────────────────────────

        types.Tool(
            name="execute_command",
            description=(
                "Execute any bash shell command on the Ubuntu 22.04 system and return its output. "
                "Use this for general-purpose commands: ls, pwd, cat, echo, python3, node, "
                "compiling code, running scripts, checking versions, or anything you would type "
                "in a terminal. Runs synchronously and waits for the result. "
                "stdout and stderr are both returned. "
                "Default timeout is 60 seconds. "
                "For long-running commands (installs, builds, servers), use stream_output instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run. Example: 'ls -la /app' or 'python3 --version'"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds before command is killed. Default is 60.",
                        "default": 60
                    }
                },
                "required": ["command"]
            }
        ),

        types.Tool(
            name="run_python_code",
            description=(
                "Execute a block of Python 3 code directly without creating a file. "
                "Great for quick calculations, data manipulation, testing logic, "
                "parsing JSON, making HTTP requests, or any Python task. "
                "All standard library modules are available. "
                "Third-party packages (numpy, pandas, etc.) can be installed first with install_package. "
                "Returns stdout, stderr, and any exceptions with traceback."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Valid Python 3 code to execute. Example: 'import json; print(json.dumps({\"key\": \"value\"}))'"
                    }
                },
                "required": ["code"]
            }
        ),

        types.Tool(
            name="kill_process",
            description=(
                "Terminate a running process by its system PID or by the process_id "
                "returned from the stream_output tool. "
                "Use force=true to send SIGKILL (immediate kill) instead of SIGTERM (graceful stop). "
                "Useful for stopping stuck processes, runaway scripts, or background jobs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "System PID as string, or the process_id returned by stream_output. Example: '1234' or 'proc_abc12345'"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "If true, force kill with SIGKILL. If false, graceful SIGTERM. Default is false.",
                        "default": False
                    }
                },
                "required": ["process_id"]
            }
        ),

        # ── GROUP 2: File Management ──────────────────────────────────────────

        types.Tool(
            name="read_file",
            description=(
                "Read and return the full contents of any text file on the filesystem. "
                "Supports .py, .js, .html, .css, .json, .yaml, .xml, .csv, .md, .sh, .env, "
                ".txt, config files, and any UTF-8 encoded text file. "
                "Optionally read only a range of lines with line_start and line_end "
                "to avoid reading very large files all at once."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file. Example: '/app/main.py'"
                    },
                    "line_start": {
                        "type": "integer",
                        "description": "First line to read (1-indexed). Optional."
                    },
                    "line_end": {
                        "type": "integer",
                        "description": "Last line to read (1-indexed). Optional."
                    }
                },
                "required": ["file_path"]
            }
        ),

        types.Tool(
            name="write_file",
            description=(
                "Create a new file or overwrite an existing file with the given content. "
                "Use this to create HTML pages, Python scripts, JSON configs, shell scripts, "
                "text documents, or any text-based file. "
                "Parent directories are created automatically if they do not exist. "
                "To make a file downloadable, write it to /app/downloads and then call get_download_url. "
                "Set append=true to add content to the end of an existing file instead of overwriting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Full path where the file will be saved. Example: '/app/downloads/report.html'"
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to write into the file."
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If true, append to existing file. If false, overwrite. Default is false.",
                        "default": False
                    }
                },
                "required": ["file_path", "content"]
            }
        ),

        types.Tool(
            name="delete_file",
            description=(
                "Permanently delete a file or directory from the filesystem. "
                "For non-empty directories, set recursive=true to delete all contents inside. "
                "WARNING: This operation is irreversible. Verify the path carefully before deleting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file or directory to delete. Example: '/app/temp/old.txt'"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, delete directory and all its contents. Default is false.",
                        "default": False
                    }
                },
                "required": ["file_path"]
            }
        ),

        types.Tool(
            name="move_file",
            description=(
                "Move or rename a file or directory to a new location. "
                "Equivalent to the Unix 'mv' command. "
                "Works for both files and directories. "
                "Destination parent directories must exist."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Current path of the file or directory. Example: '/app/old_name.py'"
                    },
                    "destination_path": {
                        "type": "string",
                        "description": "New path or name. Example: '/app/new_name.py'"
                    }
                },
                "required": ["source_path", "destination_path"]
            }
        ),

        types.Tool(
            name="copy_file",
            description=(
                "Copy a file or directory to another location. "
                "Equivalent to the Unix 'cp' command. "
                "For copying a directory and all its contents, set recursive=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Path of the file or directory to copy. Example: '/app/config.json'"
                    },
                    "destination_path": {
                        "type": "string",
                        "description": "Destination path for the copy. Example: '/app/backup/config.json'"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, copy directory and all its contents. Default is false.",
                        "default": False
                    }
                },
                "required": ["source_path", "destination_path"]
            }
        ),

        types.Tool(
            name="list_directory",
            description=(
                "List all files and subdirectories inside a given folder. "
                "Returns each item with its type (FILE or DIR), size, and permissions. "
                "Directories are listed before files, both sorted alphabetically. "
                "Set show_hidden=true to include hidden entries (names starting with dot)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "directory_path": {
                        "type": "string",
                        "description": "Path to the folder to list. Example: '/app' or '/app/downloads'"
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "If true, include hidden files and folders. Default is false.",
                        "default": False
                    }
                },
                "required": ["directory_path"]
            }
        ),

        types.Tool(
            name="create_directory",
            description=(
                "Create a new directory (folder) at the specified path. "
                "With parents=true (default), also creates all missing parent directories, "
                "equivalent to 'mkdir -p'. "
                "Does not fail if the directory already exists."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "directory_path": {
                        "type": "string",
                        "description": "Full path of the directory to create. Example: '/app/projects/myapp/src'"
                    },
                    "parents": {
                        "type": "boolean",
                        "description": "If true, create all missing parent directories. Default is true.",
                        "default": True
                    }
                },
                "required": ["directory_path"]
            }
        ),

        # ── GROUP 3: Web / Internet ───────────────────────────────────────────

        types.Tool(
            name="fetch_url",
            description=(
                "Fetch and return the content of any public URL or webpage. "
                "With extract_text=true (default), returns clean readable text by stripping "
                "HTML tags, scripts, styles, navbars, and footers. "
                "With extract_text=false, returns raw HTML. "
                "Use this to read online articles, API responses, documentation, or any web page. "
                "Pair with search_web: search first to find URLs, then fetch to read full content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to fetch. Example: 'https://docs.python.org/3/library/os.html'"
                    },
                    "extract_text": {
                        "type": "boolean",
                        "description": "If true, return clean text without HTML tags. Default is true.",
                        "default": True
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds. Default is 30.",
                        "default": 30
                    }
                },
                "required": ["url"]
            }
        ),

        types.Tool(
            name="download_file_from_url",
            description=(
                "Download a file from a URL and save it to /app/downloads. "
                "Works for any downloadable file: images, PDFs, zips, datasets, models, scripts. "
                "If filename is not provided, the original filename from the URL is used. "
                "After downloading, call get_download_url to give the user a download link. "
                "Returns the saved file path and file size in bytes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Direct URL of the file to download. Example: 'https://example.com/data.csv'"
                    },
                    "filename": {
                        "type": "string",
                        "description": "Custom name to save the file as. If omitted, uses original filename from URL."
                    }
                },
                "required": ["url"]
            }
        ),

        types.Tool(
            name="search_web",
            description=(
                "Search the web using DuckDuckGo (via the ddgs library) and return results. "
                "No API key required. No proxy required. Works directly. "
                "Returns a list of results, each with: title, URL, and a short description. "
                "Use this to find current information, news, tutorials, documentation, or any topic. "
                "Follow up with fetch_url to read the full content of any result URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string. Example: 'FastAPI async tutorial' or 'Bangladesh weather today'"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default is 10.",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        ),

        # ── GROUP 4: Package & System ─────────────────────────────────────────

        types.Tool(
            name="install_package",
            description=(
                "Install a software package using pip (Python), apt (system/Ubuntu), "
                "or npm (Node.js). "
                "Python 3, pip, Node.js 20, and npm are already pre-installed in the system. "
                "Use pip for Python libraries (numpy, flask, opencv-python, etc.). "
                "Use apt for system tools (ffmpeg, imagemagick, sqlite3, etc.). "
                "Use npm for Node.js packages (express, typescript, etc.). "
                "Returns the full installation log."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "package_name": {
                        "type": "string",
                        "description": "Name of the package. Example: 'numpy' or 'ffmpeg' or 'express'"
                    },
                    "manager": {
                        "type": "string",
                        "enum": ["pip", "apt", "npm"],
                        "description": "'pip' for Python packages, 'apt' for system packages, 'npm' for Node.js packages."
                    }
                },
                "required": ["package_name", "manager"]
            }
        ),

        types.Tool(
            name="get_system_info",
            description=(
                "Return a full snapshot of the system's current state. "
                "Includes: CPU usage percentage, total and free RAM, "
                "disk total/used/free, Python version, Node.js version, "
                "npm version, current working directory, and hostname. "
                "Call this before starting heavy tasks to check available resources."
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),

        types.Tool(
            name="list_processes",
            description=(
                "List all currently running processes on the system. "
                "Returns PID, name, CPU%, memory%, and status for each process. "
                "Use filter_name to narrow results to a specific program. "
                "Useful for finding PIDs to kill, monitoring CPU/memory hogs, "
                "or checking if a server or script is still running."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter_name": {
                        "type": "string",
                        "description": "Filter by process name (case-insensitive). Example: 'python' or 'node'. Leave empty for all processes."
                    }
                }
            }
        ),

        types.Tool(
            name="check_disk_space",
            description=(
                "Check how much disk space is available on the filesystem. "
                "Returns total size, used space, free space, and usage percentage "
                "for the given path. "
                "Check this before downloading large files or creating archives "
                "to avoid running out of space."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Filesystem path to check. Default is '/' (root).",
                        "default": "/"
                    }
                }
            }
        ),

        types.Tool(
            name="view_logs",
            description=(
                "Read the last N lines from a log file. "
                "Works with any log file: /var/log/syslog, /var/log/auth.log, "
                "application log files, or custom log paths. "
                "Returns the most recent log entries. "
                "Useful for debugging errors or monitoring application activity."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "log_file": {
                        "type": "string",
                        "description": "Absolute path to the log file. Example: '/app/app.log' or '/var/log/syslog'",
                        "default": "/var/log/syslog"
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of recent lines to return. Default is 50.",
                        "default": 50
                    }
                }
            }
        ),

        # ── GROUP 5: Text Processing ──────────────────────────────────────────

        types.Tool(
            name="grep_file",
            description=(
                "Search for a text pattern inside a file or directory of files. "
                "Works like the Unix 'grep -n' command, returning matched lines with line numbers. "
                "Set recursive=true to search all files inside a directory. "
                "Set case_sensitive=false for case-insensitive matching. "
                "Supports regular expressions for advanced pattern matching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex to search for. Example: 'def main' or 'ERROR' or '^import'"
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory path to search in. Example: '/app/main.py' or '/app'"
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "If false, match regardless of letter case. Default is true.",
                        "default": True
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true and path is a directory, search all files inside recursively. Default is false.",
                        "default": False
                    }
                },
                "required": ["pattern", "path"]
            }
        ),

        types.Tool(
            name="find_files",
            description=(
                "Find files or directories by name pattern inside a directory. "
                "Works like the Unix 'find' command. "
                "Supports glob patterns: '*.py' finds all Python files, "
                "'config*' finds anything starting with config. "
                "Returns full paths of all matches."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory to search inside. Example: '/app'"
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Filename glob pattern. Example: '*.py' or 'test_*' or '*.html'"
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["file", "directory", "both"],
                        "description": "Search for files only, directories only, or both. Default is 'both'.",
                        "default": "both"
                    }
                },
                "required": ["directory", "pattern"]
            }
        ),

        types.Tool(
            name="replace_in_file",
            description=(
                "Find and replace all occurrences of a text string inside a file. "
                "The file is modified in place. "
                "Set use_regex=true to use regular expressions for advanced replacements. "
                "Returns the number of replacements made. "
                "Use this to edit config files, update version numbers, fix typos in code, etc."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to edit. Example: '/app/config.json'"
                    },
                    "search_text": {
                        "type": "string",
                        "description": "The text (or regex pattern) to find."
                    },
                    "replacement_text": {
                        "type": "string",
                        "description": "The text to replace it with."
                    },
                    "use_regex": {
                        "type": "boolean",
                        "description": "If true, treat search_text as a regular expression. Default is false.",
                        "default": False
                    }
                },
                "required": ["file_path", "search_text", "replacement_text"]
            }
        ),

        # ── GROUP 6: Git ──────────────────────────────────────────────────────

        types.Tool(
            name="git_clone",
            description=(
                "Clone a Git repository from a remote URL to the local filesystem. "
                "Works with GitHub, GitLab, Bitbucket, and any Git-compatible host. "
                "For private repositories, include credentials in the URL: "
                "'https://username:token@github.com/user/repo.git'. "
                "Optionally clone a specific branch with the branch parameter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": "Git repository URL. Example: 'https://github.com/username/repo.git'"
                    },
                    "destination": {
                        "type": "string",
                        "description": "Local path to clone into. If omitted, clones into /app with repo name."
                    },
                    "branch": {
                        "type": "string",
                        "description": "Specific branch to clone. If omitted, clones the default branch."
                    }
                },
                "required": ["repo_url"]
            }
        ),

        types.Tool(
            name="git_status",
            description=(
                "Show the working tree status of a Git repository. "
                "Returns the current branch, staged files, unstaged changes, "
                "and untracked files. "
                "Run this before committing to see what has changed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the Git repository root. Default is '/app'.",
                        "default": "/app"
                    }
                }
            }
        ),

        types.Tool(
            name="git_commit",
            description=(
                "Stage all changes (git add -A) and create a commit with the given message. "
                "Commits all modified, added, and deleted files in the repository. "
                "Make sure you are inside a valid Git repository. "
                "Returns the commit hash and a summary of changes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Commit message. Example: 'Fix login bug' or 'Add search feature'"
                    },
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the Git repository. Default is '/app'.",
                        "default": "/app"
                    }
                },
                "required": ["message"]
            }
        ),

        types.Tool(
            name="git_push",
            description=(
                "Push local commits to the remote Git repository. "
                "The repository must have a remote configured (typically 'origin'). "
                "For authentication, embed credentials in the remote URL or "
                "set GIT_ASKPASS / GITHUB_TOKEN environment variables beforehand."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the Git repository. Default is '/app'.",
                        "default": "/app"
                    },
                    "remote": {
                        "type": "string",
                        "description": "Remote name to push to. Default is 'origin'.",
                        "default": "origin"
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch to push. If omitted, pushes the current branch."
                    }
                }
            }
        ),

        types.Tool(
            name="git_pull",
            description=(
                "Pull the latest commits from the remote repository into the local branch. "
                "Fetches and merges changes from the remote. "
                "Returns the list of updated files or any merge conflict messages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Path to the Git repository. Default is '/app'.",
                        "default": "/app"
                    },
                    "remote": {
                        "type": "string",
                        "description": "Remote name to pull from. Default is 'origin'.",
                        "default": "origin"
                    }
                }
            }
        ),

        # ── GROUP 7: Archive ──────────────────────────────────────────────────

        types.Tool(
            name="compress_files",
            description=(
                "Compress a file or directory into a zip or tar.gz archive. "
                "The output archive is always saved to /app/downloads. "
                "After compression, call get_download_url to give the user a download link. "
                "Use zip format for cross-platform compatibility. "
                "Use tar.gz for Linux/Mac and typically smaller file sizes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Path to the file or directory to compress. Example: '/app/myproject'"
                    },
                    "output_filename": {
                        "type": "string",
                        "description": "Name of the output archive (just the filename, not full path). Example: 'myproject.zip'"
                    },
                    "format": {
                        "type": "string",
                        "enum": ["zip", "tar.gz"],
                        "description": "Archive format. Default is 'zip'.",
                        "default": "zip"
                    }
                },
                "required": ["source_path", "output_filename"]
            }
        ),

        types.Tool(
            name="extract_archive",
            description=(
                "Extract a .zip, .tar.gz, .tar.bz2, or .tar archive to a directory. "
                "If destination is not specified, extracts to the same directory as the archive. "
                "Returns the number of extracted files and the destination path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "archive_path": {
                        "type": "string",
                        "description": "Full path to the archive file. Example: '/app/downloads/project.zip'"
                    },
                    "destination": {
                        "type": "string",
                        "description": "Directory to extract into. If omitted, extracts next to the archive."
                    }
                },
                "required": ["archive_path"]
            }
        ),

        # ── GROUP 8: Network ──────────────────────────────────────────────────

        types.Tool(
            name="ping_host",
            description=(
                "Ping a hostname or IP address to check if it is reachable. "
                "Returns whether the host responded, average round-trip time in ms, "
                "and packet loss percentage. "
                "Use this to verify internet connectivity or check if a remote server is up."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Hostname or IP to ping. Example: 'google.com' or '8.8.8.8'"
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of packets to send. Default is 4.",
                        "default": 4
                    }
                },
                "required": ["host"]
            }
        ),

        types.Tool(
            name="check_port",
            description=(
                "Check if a TCP port is open on a given host. "
                "Returns OPEN or CLOSED and the connection response time. "
                "Use this to verify if a web server (port 80/443), database (5432/3306), "
                "SSH (22), or any other service is accepting connections."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "Hostname or IP to check. Example: 'localhost' or 'example.com'"
                    },
                    "port": {
                        "type": "integer",
                        "description": "Port number to check. Example: 80, 443, 22, 5432, 3306"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Connection timeout in seconds. Default is 5.",
                        "default": 5
                    }
                },
                "required": ["host", "port"]
            }
        ),

        types.Tool(
            name="get_ip_info",
            description=(
                "Look up geolocation and network info for an IP address or domain. "
                "Returns country, city, region, ISP, timezone, and coordinates. "
                "Leave ip_or_domain empty to get info about this machine's own public IP address."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ip_or_domain": {
                        "type": "string",
                        "description": "IP address or domain to look up. Leave empty for own public IP. Example: '8.8.8.8' or 'github.com'"
                    }
                }
            }
        ),

        types.Tool(
            name="http_request",
            description=(
                "Send a custom HTTP request to any URL with full control over method, headers, and body. "
                "Supports GET, POST, PUT, DELETE, PATCH. "
                "Use this to interact with REST APIs, send form data, test webhooks, "
                "or make any HTTP call. "
                "Returns HTTP status code, response headers, and response body."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Target URL. Example: 'https://api.example.com/v1/data'"
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                        "description": "HTTP method. Default is 'GET'.",
                        "default": "GET"
                    },
                    "headers": {
                        "type": "object",
                        "description": "Request headers as key-value pairs. Example: {\"Authorization\": \"Bearer token123\"}"
                    },
                    "body": {
                        "type": "string",
                        "description": "Request body string (for POST/PUT/PATCH). Stringify JSON before passing."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Default is 30.",
                        "default": 30
                    }
                },
                "required": ["url"]
            }
        ),

        # ── GROUP 9: Permissions & Environment ───────────────────────────────

        types.Tool(
            name="change_permissions",
            description=(
                "Change file or directory permissions using chmod syntax. "
                "Accepts octal notation: '755' (owner rwx, others rx), '644' (owner rw, others r), "
                "'777' (everyone rwx). "
                "Also accepts symbolic: '+x' (add execute), 'u+w' (add write for owner). "
                "Set recursive=true to apply to all files inside a directory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file or directory. Example: '/app/run.sh'"
                    },
                    "permissions": {
                        "type": "string",
                        "description": "Permission string. Example: '755' or '+x' or '644'"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, apply recursively to directory contents. Default is false.",
                        "default": False
                    }
                },
                "required": ["file_path", "permissions"]
            }
        ),

        types.Tool(
            name="get_env_variable",
            description=(
                "Read the value of an environment variable. "
                "If variable_name is provided, returns that specific variable's value. "
                "If variable_name is omitted, returns all environment variables as a list. "
                "Use this to check PATH, HOME, API keys, and other configuration values."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "variable_name": {
                        "type": "string",
                        "description": "Name of the env variable to read. Example: 'PATH' or 'HOME'. Leave empty to list all."
                    }
                }
            }
        ),

        types.Tool(
            name="set_env_variable",
            description=(
                "Set an environment variable for the current server session. "
                "The variable is available to all commands and tools called afterward. "
                "Use this to configure API keys, database URLs, feature flags, "
                "or any runtime settings before executing commands that need them."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "variable_name": {
                        "type": "string",
                        "description": "Name of the variable to set. Example: 'DATABASE_URL' or 'API_KEY'"
                    },
                    "value": {
                        "type": "string",
                        "description": "Value to assign. Example: 'postgres://localhost/mydb'"
                    }
                },
                "required": ["variable_name", "value"]
            }
        ),

        # ── GROUP 10: Terminal State ──────────────────────────────────────────

        types.Tool(
            name="clear_terminal",
            description=(
                "Reset the terminal state completely. "
                "Clears: all stream_output output buffers, all tracked process history, "
                "all process read indices, and all temporary files in /app/temp. "
                "Does NOT affect files in /app/downloads or any user-created files. "
                "Call this to free memory from old output buffers or to start fresh."
            ),
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),

        types.Tool(
            name="stream_output",
            description=(
                "Start a long-running command in the background and return a process_id immediately. "
                "The command runs asynchronously; its output is captured in a buffer. "
                "Use get_process_output with the returned process_id to read output at any time. "
                "Poll get_process_output repeatedly until status becomes 'finished' or 'error'. "
                "To stop the command early, call kill_process with the process_id. "
                "Best used for: apt install, pip install, npm install, long build scripts, "
                "server startup, or any command that takes more than a few seconds."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run in background. Example: 'apt-get install -y ffmpeg'"
                    }
                },
                "required": ["command"]
            }
        ),

        types.Tool(
            name="get_process_output",
            description=(
                "Retrieve the current output of a background process started by stream_output. "
                "Returns all captured output lines and the current process status. "
                "Status values: 'running' (still executing), 'finished' (completed OK), 'error' (failed). "
                "Set get_new_only=true to get only lines added since the last call to this tool "
                "(useful for polling in a loop without re-reading old output). "
                "Keep calling until status is 'finished' or 'error' to get the complete output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "process_id": {
                        "type": "string",
                        "description": "The process_id string returned by stream_output. Example: 'proc_a1b2c3d4'"
                    },
                    "get_new_only": {
                        "type": "boolean",
                        "description": "If true, return only new lines since last call. Default is false.",
                        "default": False
                    }
                },
                "required": ["process_id"]
            }
        ),

        # ── GROUP 11: Download URL ────────────────────────────────────────────

        types.Tool(
            name="get_download_url",
            description=(
                "Generate a public download URL for a file in /app/downloads. "
                "Clicking the returned URL in any browser will immediately trigger a file download. "
                "The URL is served with Content-Disposition: attachment, so the browser downloads "
                "the file automatically instead of displaying it. "
                "The file must exist in /app/downloads before calling this tool. "
                "Works for any file type: .zip, .html, .pdf, .py, .csv, .json, .txt, etc. "
                "Workflow: write_file or compress_files → save to /app/downloads → get_download_url → share link."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Filename inside /app/downloads. Example: 'report.zip' or 'index.html'"
                    }
                },
                "required": ["filename"]
            }
        ),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# MCP: call tool
# ─────────────────────────────────────────────────────────────────────────────
@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}

    def ok(text: str) -> list[types.TextContent]:
        return [types.TextContent(type="text", text=str(text))]

    try:
        # ── execute_command ───────────────────────────────────────────────────
        if name == "execute_command":
            command = args["command"]
            timeout = int(args.get("timeout", 60))
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout
            )
            output = result.stdout
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            if not output.strip():
                output = f"[Command completed with exit code {result.returncode}]"
            return ok(output)

        # ── run_python_code ───────────────────────────────────────────────────
        elif name == "run_python_code":
            code = args["code"]
            tmp = os.path.join(TEMP_DIR, f"script_{uuid.uuid4().hex[:8]}.py")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(code)
            result = subprocess.run(
                ["python3", tmp], capture_output=True, text=True, timeout=60
            )
            os.remove(tmp)
            output = result.stdout
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            return ok(output or "[No output]")

        # ── kill_process ──────────────────────────────────────────────────────
        elif name == "kill_process":
            pid_str = args["process_id"]
            force = args.get("force", False)
            if pid_str in active_processes:
                proc = active_processes[pid_str]
                proc.kill() if force else proc.terminate()
                process_status[pid_str] = "error"
                active_processes.pop(pid_str, None)
                return ok(f"Background process '{pid_str}' terminated.")
            sig = "-9" if force else "-15"
            result = subprocess.run(
                f"kill {sig} {pid_str}", shell=True, capture_output=True, text=True
            )
            if result.returncode == 0:
                return ok(f"Process {pid_str} killed.")
            return ok(f"Failed: {result.stderr.strip()}")

        # ── read_file ─────────────────────────────────────────────────────────
        elif name == "read_file":
            fp = args["file_path"]
            if not os.path.exists(fp):
                return ok(f"Error: File not found: {fp}")
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            s = args.get("line_start")
            e = args.get("line_end")
            if s or e:
                lines = lines[(s or 1) - 1: (e or len(lines))]
            return ok("".join(lines))

        # ── write_file ────────────────────────────────────────────────────────
        elif name == "write_file":
            fp = args["file_path"]
            content = args["content"]
            append = args.get("append", False)
            Path(fp).parent.mkdir(parents=True, exist_ok=True)
            with open(fp, "a" if append else "w", encoding="utf-8") as f:
                f.write(content)
            size = os.path.getsize(fp)
            action = "Appended to" if append else "Written"
            return ok(f"{action}: {fp} ({size} bytes)")

        # ── delete_file ───────────────────────────────────────────────────────
        elif name == "delete_file":
            fp = args["file_path"]
            recursive = args.get("recursive", False)
            if not os.path.exists(fp):
                return ok(f"Error: Path not found: {fp}")
            if os.path.isfile(fp) or os.path.islink(fp):
                os.remove(fp)
                return ok(f"File deleted: {fp}")
            if recursive:
                shutil.rmtree(fp)
                return ok(f"Directory deleted: {fp}")
            try:
                os.rmdir(fp)
                return ok(f"Empty directory deleted: {fp}")
            except OSError:
                return ok("Directory is not empty. Use recursive=true to delete non-empty directories.")

        # ── move_file ─────────────────────────────────────────────────────────
        elif name == "move_file":
            src, dst = args["source_path"], args["destination_path"]
            if not os.path.exists(src):
                return ok(f"Error: Source not found: {src}")
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            return ok(f"Moved: {src} -> {dst}")

        # ── copy_file ─────────────────────────────────────────────────────────
        elif name == "copy_file":
            src, dst = args["source_path"], args["destination_path"]
            recursive = args.get("recursive", False)
            if not os.path.exists(src):
                return ok(f"Error: Source not found: {src}")
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            if os.path.isdir(src):
                if not recursive:
                    return ok("Source is a directory. Use recursive=true.")
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return ok(f"Copied: {src} -> {dst}")

        # ── list_directory ────────────────────────────────────────────────────
        elif name == "list_directory":
            dp = args["directory_path"]
            show_hidden = args.get("show_hidden", False)
            if not os.path.exists(dp):
                return ok(f"Error: Directory not found: {dp}")
            rows = []
            for entry in sorted(os.scandir(dp), key=lambda e: (not e.is_dir(), e.name)):
                if not show_hidden and entry.name.startswith("."):
                    continue
                kind = "DIR " if entry.is_dir() else "FILE"
                st = entry.stat()
                sz = st.st_size
                if sz < 1024:
                    size_str = f"{sz}B"
                elif sz < 1024 ** 2:
                    size_str = f"{sz // 1024}KB"
                else:
                    size_str = f"{sz // 1024 ** 2}MB"
                perm = oct(stat.S_IMODE(st.st_mode))[2:]
                rows.append(f"[{kind}] {entry.name:<45} {size_str:<10} {perm}")
            if not rows:
                return ok(f"Empty directory: {dp}")
            return ok(f"Contents of {dp}:\n{'=' * 65}\n" + "\n".join(rows))

        # ── create_directory ──────────────────────────────────────────────────
        elif name == "create_directory":
            dp = args["directory_path"]
            parents = args.get("parents", True)
            Path(dp).mkdir(parents=parents, exist_ok=True)
            return ok(f"Directory created: {dp}")

        # ── fetch_url ─────────────────────────────────────────────────────────
        elif name == "fetch_url":
            url = args["url"]
            extract = args.get("extract_text", True)
            timeout = int(args.get("timeout", 30))
            headers = {"User-Agent": "Mozilla/5.0 (AI-Terminal/1.0)"}
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if extract:
                soup = BeautifulSoup(resp.text, "lxml")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                lines = [ln for ln in text.splitlines() if ln.strip()]
                return ok("\n".join(lines[:600]))
            return ok(resp.text[:60000])

        # ── download_file_from_url ────────────────────────────────────────────
        elif name == "download_file_from_url":
            url = args["url"]
            filename = args.get("filename") or url.split("/")[-1].split("?")[0] or "file"
            dest = os.path.join(DOWNLOADS_DIR, filename)
            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            size = os.path.getsize(dest)
            return ok(f"Downloaded: {filename}\nPath: {dest}\nSize: {size} bytes")

        # ── search_web ────────────────────────────────────────────────────────
        elif name == "search_web":
            query = args["query"]
            max_results = int(args.get("max_results", 10))
            ddgs = DDGS()
            raw = list(ddgs.text(query, max_results=max_results))
            if not raw:
                return ok("No results found.")
            lines = [f"Search results for: {query}\n{'=' * 60}"]
            for r in raw:
                lines.append(
                    f"Title: {r.get('title', 'N/A')}\n"
                    f"URL:   {r.get('href', 'N/A')}\n"
                    f"Desc:  {r.get('body', 'N/A')}\n"
                    + "─" * 50
                )
            return ok("\n".join(lines))

        # ── install_package ───────────────────────────────────────────────────
        elif name == "install_package":
            pkg = args["package_name"]
            mgr = args["manager"]
            cmds = {"pip": f"pip3 install {pkg}", "apt": f"apt-get install -y {pkg}", "npm": f"npm install -g {pkg}"}
            if mgr not in cmds:
                return ok(f"Unknown manager: {mgr}. Use pip, apt, or npm.")
            result = subprocess.run(cmds[mgr], shell=True, capture_output=True, text=True, timeout=180)
            return ok(result.stdout + result.stderr or f"{pkg} installation done.")

        # ── get_system_info ───────────────────────────────────────────────────
        elif name == "get_system_info":
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            py_v = subprocess.run(["python3", "--version"], capture_output=True, text=True).stdout.strip()
            nd_v = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
            npm_v = subprocess.run(["npm", "--version"], capture_output=True, text=True).stdout.strip()
            return ok(
                f"System Information\n{'=' * 40}\n"
                f"OS          : Ubuntu 22.04\n"
                f"CPU Usage   : {cpu}%\n"
                f"RAM Total   : {mem.total // 1024 ** 2} MB\n"
                f"RAM Used    : {mem.used // 1024 ** 2} MB\n"
                f"RAM Free    : {mem.available // 1024 ** 2} MB ({100 - mem.percent:.1f}% free)\n"
                f"Disk Total  : {disk.total // 1024 ** 3} GB\n"
                f"Disk Used   : {disk.used // 1024 ** 3} GB\n"
                f"Disk Free   : {disk.free // 1024 ** 3} GB ({100 - disk.percent:.1f}% free)\n"
                f"Python      : {py_v}\n"
                f"Node.js     : {nd_v}\n"
                f"npm         : {npm_v}\n"
                f"Working Dir : {os.getcwd()}\n"
                f"Hostname    : {socket.gethostname()}\n"
            )

        # ── list_processes ────────────────────────────────────────────────────
        elif name == "list_processes":
            fn = args.get("filter_name", "").lower()
            rows = []
            for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
                try:
                    info = proc.info
                    if fn and fn not in info["name"].lower():
                        continue
                    rows.append(
                        f"PID:{info['pid']:<7} NAME:{info['name']:<22} "
                        f"CPU:{info['cpu_percent']:<6} MEM:{info['memory_percent']:.1f}%  STATUS:{info['status']}"
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return ok("Running Processes:\n" + "=" * 75 + "\n" + "\n".join(rows) if rows else "No processes found.")

        # ── check_disk_space ──────────────────────────────────────────────────
        elif name == "check_disk_space":
            path = args.get("path", "/")
            d = psutil.disk_usage(path)
            return ok(
                f"Disk Usage: {path}\n{'=' * 40}\n"
                f"Total : {d.total // 1024 ** 3} GB\n"
                f"Used  : {d.used // 1024 ** 3} GB\n"
                f"Free  : {d.free // 1024 ** 3} GB\n"
                f"Usage : {d.percent}%\n"
            )

        # ── view_logs ─────────────────────────────────────────────────────────
        elif name == "view_logs":
            log_file = args.get("log_file", "/var/log/syslog")
            lines = int(args.get("lines", 50))
            result = subprocess.run(f"tail -n {lines} {log_file}", shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                return ok(f"Error reading log: {result.stderr}")
            return ok(result.stdout or "[Log file is empty]")

        # ── grep_file ─────────────────────────────────────────────────────────
        elif name == "grep_file":
            pattern = args["pattern"]
            path = args["path"]
            ci = "" if args.get("case_sensitive", True) else "-i"
            rec = "-r" if args.get("recursive", False) else ""
            result = subprocess.run(
                f"grep -n {ci} {rec} '{pattern}' '{path}'",
                shell=True, capture_output=True, text=True
            )
            return ok(result.stdout or f"No matches for '{pattern}' in {path}")

        # ── find_files ────────────────────────────────────────────────────────
        elif name == "find_files":
            directory = args["directory"]
            pattern = args["pattern"]
            ft = args.get("file_type", "both")
            type_flag = {
                "file": "-type f", "directory": "-type d", "both": ""
            }.get(ft, "")
            result = subprocess.run(
                f"find '{directory}' {type_flag} -name '{pattern}'",
                shell=True, capture_output=True, text=True
            )
            return ok(result.stdout.strip() or f"No files matching '{pattern}' in {directory}")

        # ── replace_in_file ───────────────────────────────────────────────────
        elif name == "replace_in_file":
            fp = args["file_path"]
            search = args["search_text"]
            replace = args["replacement_text"]
            use_regex = args.get("use_regex", False)
            if not os.path.exists(fp):
                return ok(f"Error: File not found: {fp}")
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
            if use_regex:
                new_content, count = re.subn(search, replace, content)
            else:
                count = content.count(search)
                new_content = content.replace(search, replace)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(new_content)
            return ok(f"Replaced {count} occurrence(s) in {fp}")

        # ── git_clone ─────────────────────────────────────────────────────────
        elif name == "git_clone":
            url = args["repo_url"]
            dest = args.get("destination", "/app")
            branch = args.get("branch", "")
            b_flag = f"--branch {branch}" if branch else ""
            result = subprocess.run(
                f"git clone {b_flag} {url} {dest}",
                shell=True, capture_output=True, text=True, timeout=180
            )
            return ok(result.stdout + result.stderr)

        # ── git_status ────────────────────────────────────────────────────────
        elif name == "git_status":
            path = args.get("repo_path", "/app")
            result = subprocess.run("git status", shell=True, capture_output=True, text=True, cwd=path)
            return ok(result.stdout + result.stderr)

        # ── git_commit ────────────────────────────────────────────────────────
        elif name == "git_commit":
            msg = args["message"]
            path = args.get("repo_path", "/app")
            add = subprocess.run("git add -A", shell=True, capture_output=True, text=True, cwd=path)
            commit = subprocess.run(
                f'git commit -m "{msg}"', shell=True, capture_output=True, text=True, cwd=path
            )
            return ok(add.stdout + commit.stdout + commit.stderr)

        # ── git_push ──────────────────────────────────────────────────────────
        elif name == "git_push":
            path = args.get("repo_path", "/app")
            remote = args.get("remote", "origin")
            branch = args.get("branch", "")
            result = subprocess.run(
                f"git push {remote} {branch}".strip(),
                shell=True, capture_output=True, text=True, cwd=path, timeout=60
            )
            return ok(result.stdout + result.stderr)

        # ── git_pull ──────────────────────────────────────────────────────────
        elif name == "git_pull":
            path = args.get("repo_path", "/app")
            remote = args.get("remote", "origin")
            result = subprocess.run(
                f"git pull {remote}", shell=True, capture_output=True, text=True, cwd=path, timeout=60
            )
            return ok(result.stdout + result.stderr)

        # ── compress_files ────────────────────────────────────────────────────
        elif name == "compress_files":
            src = args["source_path"]
            out_name = args["output_filename"]
            fmt = args.get("format", "zip")
            out_path = os.path.join(DOWNLOADS_DIR, out_name)
            if not os.path.exists(src):
                return ok(f"Error: Source not found: {src}")
            if fmt == "zip":
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    if os.path.isdir(src):
                        for root, _, files in os.walk(src):
                            for file in files:
                                full = os.path.join(root, file)
                                zf.write(full, os.path.relpath(full, os.path.dirname(src)))
                    else:
                        zf.write(src, os.path.basename(src))
            else:
                with tarfile.open(out_path, "w:gz") as tf:
                    tf.add(src, arcname=os.path.basename(src))
            size = os.path.getsize(out_path)
            return ok(f"Archive created: {out_name}\nPath: {out_path}\nSize: {size} bytes")

        # ── extract_archive ───────────────────────────────────────────────────
        elif name == "extract_archive":
            arc = args["archive_path"]
            dest = args.get("destination", os.path.dirname(arc))
            if not os.path.exists(arc):
                return ok(f"Error: Archive not found: {arc}")
            os.makedirs(dest, exist_ok=True)
            if arc.endswith(".zip"):
                with zipfile.ZipFile(arc, "r") as zf:
                    zf.extractall(dest)
                    count = len(zf.namelist())
            elif arc.endswith((".tar.gz", ".tar.bz2", ".tar")):
                with tarfile.open(arc, "r:*") as tf:
                    tf.extractall(dest)
                    count = len(tf.getnames())
            else:
                return ok(f"Unsupported format: {arc}")
            return ok(f"Extracted {count} files to: {dest}")

        # ── ping_host ─────────────────────────────────────────────────────────
        elif name == "ping_host":
            host = args["host"]
            count = int(args.get("count", 4))
            result = subprocess.run(
                f"ping -c {count} {host}", shell=True, capture_output=True, text=True, timeout=30
            )
            return ok(result.stdout + result.stderr)

        # ── check_port ────────────────────────────────────────────────────────
        elif name == "check_port":
            host = args["host"]
            port = int(args["port"])
            timeout = int(args.get("timeout", 5))
            t0 = time.time()
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                r = s.connect_ex((host, port))
                elapsed = round((time.time() - t0) * 1000, 2)
                s.close()
                status = "OPEN" if r == 0 else "CLOSED"
                return ok(f"Port {port} on {host} is {status} (response: {elapsed}ms)")
            except Exception as e:
                return ok(f"Error: {e}")

        # ── get_ip_info ───────────────────────────────────────────────────────
        elif name == "get_ip_info":
            target = args.get("ip_or_domain", "")
            url = f"https://ipinfo.io/{target}/json" if target else "https://ipinfo.io/json"
            resp = requests.get(url, timeout=10)
            data = resp.json()
            return ok("\n".join(f"{k}: {v}" for k, v in data.items()))

        # ── http_request ──────────────────────────────────────────────────────
        elif name == "http_request":
            url = args["url"]
            method = args.get("method", "GET").upper()
            headers = args.get("headers") or {}
            body = args.get("body")
            timeout = int(args.get("timeout", 30))
            resp = requests.request(method, url, headers=headers, data=body, timeout=timeout)
            return ok(
                f"Status: {resp.status_code}\n"
                f"Headers: {dict(resp.headers)}\n"
                f"Body:\n{resp.text[:10000]}"
            )

        # ── change_permissions ────────────────────────────────────────────────
        elif name == "change_permissions":
            fp = args["file_path"]
            perms = args["permissions"]
            rec = "-R " if args.get("recursive", False) else ""
            result = subprocess.run(
                f"chmod {rec}{perms} '{fp}'", shell=True, capture_output=True, text=True
            )
            if result.returncode == 0:
                return ok(f"Permissions set to {perms} for {fp}")
            return ok(f"Error: {result.stderr.strip()}")

        # ── get_env_variable ──────────────────────────────────────────────────
        elif name == "get_env_variable":
            var = args.get("variable_name")
            if var:
                val = os.environ.get(var)
                return ok(f"{var}={val}" if val is not None else f"'{var}' is not set.")
            return ok("\n".join(f"{k}={v}" for k, v in sorted(os.environ.items())))

        # ── set_env_variable ──────────────────────────────────────────────────
        elif name == "set_env_variable":
            var = args["variable_name"]
            val = args["value"]
            os.environ[var] = val
            return ok(f"Set: {var}={val}")

        # ── clear_terminal ────────────────────────────────────────────────────
        elif name == "clear_terminal":
            process_buffer.clear()
            process_status.clear()
            process_read_index.clear()
            for item in Path(TEMP_DIR).glob("*"):
                try:
                    item.unlink() if item.is_file() else shutil.rmtree(item)
                except Exception:
                    pass
            return ok("Terminal cleared. Buffers, process history, and temp files removed.")

        # ── stream_output ─────────────────────────────────────────────────────
        elif name == "stream_output":
            command = args["command"]
            pid = f"proc_{uuid.uuid4().hex[:8]}"
            process_buffer[pid] = []
            process_status[pid] = "running"
            process_read_index[pid] = 0
            threading.Thread(target=_run_background, args=(pid, command), daemon=True).start()
            return ok(
                f"Background process started.\n"
                f"process_id : {pid}\n"
                f"status     : running\n"
                f"command    : {command}\n"
                f"Use get_process_output with process_id='{pid}' to read output."
            )

        # ── get_process_output ────────────────────────────────────────────────
        elif name == "get_process_output":
            pid = args["process_id"]
            new_only = args.get("get_new_only", False)
            if pid not in process_buffer:
                return ok(f"No process found with id: {pid}")
            status = process_status.get(pid, "unknown")
            if new_only:
                last = process_read_index.get(pid, 0)
                lines = process_buffer[pid][last:]
                process_read_index[pid] = len(process_buffer[pid])
            else:
                lines = process_buffer[pid]
            output = "\n".join(lines) if lines else "[No output yet]"
            return ok(f"process_id : {pid}\nstatus     : {status}\n\nOutput:\n{output}")

        # ── get_download_url ──────────────────────────────────────────────────
        elif name == "get_download_url":
            filename = args["filename"]
            fp = os.path.join(DOWNLOADS_DIR, filename)
            if not os.path.exists(fp):
                return ok(
                    f"File not found in /app/downloads: {filename}\n"
                    "Create the file first using write_file or compress_files."
                )
            base = os.environ.get("SPACE_HOST", "your-username-your-space.hf.space")
            if not base.startswith("http"):
                base = f"https://{base}"
            url = f"{base}/download/{filename}"
            size = os.path.getsize(fp)
            return ok(
                f"Download URL : {url}\n"
                f"Filename     : {filename}\n"
                f"Size         : {size} bytes\n"
                f"Clicking the URL will automatically start the file download."
            )

        else:
            return ok(f"Unknown tool: {name}")

    except Exception as exc:
        return ok(f"[Tool error in '{name}']: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
