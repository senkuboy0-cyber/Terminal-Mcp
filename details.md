# Terminal MCP Server - Details

## Overview

Terminal MCP Server is a Model Context Protocol (MCP) compatible server that provides AI assistants with comprehensive terminal and system operations capabilities through a standardized tool interface.

## Architecture

```
┌─────────────────┐     MCP Protocol      ┌──────────────────┐
│   AI Assistant  │◄──────────────────────►│  MCP Server      │
│   (Claude, etc) │                       │  (FastAPI)       │
└─────────────────┘                       └────────┬─────────┘
                                                 │
                              ┌──────────────────┼──────────────────┐
                              │                  │                  │
                        ┌─────▼─────┐    ┌──────▼──────┐    ┌──────▼──────┐
                        │  Ubuntu   │    │   System    │    │   Network   │
                        │  22.04    │    │  (psutil)   │    │  (requests) │
                        └───────────┘    └─────────────┘    └─────────────┘
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check - returns "mcp online" |
| `/sse` | GET | SSE connection for MCP clients |
| `/messages/` | POST | MCP message handling |
| `/download/{filename}` | GET | File download endpoint |

## Tools Reference

### Group 1: Command Execution

#### execute_command
Execute bash commands synchronously.
- **Parameters**: `command` (string), `timeout` (integer, default: 60)
- **Returns**: stdout + stderr output

#### run_python_code
Execute Python 3 code without creating files.
- **Parameters**: `code` (string)
- **Returns**: stdout, stderr, and exceptions

#### kill_process
Terminate processes by PID.
- **Parameters**: `process_id` (string), `force` (boolean)
- **Returns**: Confirmation message

#### stream_output
Start background commands.
- **Parameters**: `command` (string)
- **Returns**: `process_id` for tracking

#### get_process_output
Read background process output.
- **Parameters**: `process_id` (string), `get_new_only` (boolean)
- **Returns**: Output lines and status

### Group 2: File Management

#### read_file
Read file contents with optional line range.
- **Parameters**: `file_path`, `line_start`, `line_end`
- **Returns**: File contents

#### write_file
Create or overwrite files.
- **Parameters**: `file_path`, `content`, `append` (boolean)
- **Returns**: Confirmation with file size

#### delete_file
Delete files or directories.
- **Parameters**: `file_path`, `recursive` (boolean)
- **Returns**: Confirmation

#### move_file / copy_file
Move or copy files and directories.
- **Parameters**: `source_path`, `destination_path`, `recursive`

#### list_directory
List directory contents.
- **Parameters**: `directory_path`, `show_hidden`
- **Returns**: Files/dirs with size and permissions

#### create_directory
Create directories with parent support.
- **Parameters**: `directory_path`, `parents` (boolean)

### Group 3: Web & Internet

#### fetch_url
Fetch webpage content.
- **Parameters**: `url`, `extract_text` (boolean), `timeout`
- **Returns**: HTML or clean text

#### download_file_from_url
Download files to /app/downloads.
- **Parameters**: `url`, `filename` (optional)

#### search_web
DuckDuckGo web search.
- **Parameters**: `query`, `max_results`
- **Returns**: Title, URL, description

#### http_request
Custom HTTP requests.
- **Parameters**: `url`, `method`, `headers`, `body`, `timeout`
- **Returns**: Status, headers, body

### Group 4: Package & System

#### install_package
Install packages via pip/apt/npm.
- **Parameters**: `package_name`, `manager` (pip/apt/npm)

#### get_system_info
System snapshot.
- **Returns**: CPU, RAM, disk, Python/Node versions

#### list_processes
List running processes.
- **Parameters**: `filter_name` (optional)

#### check_disk_space
Disk usage info.
- **Parameters**: `path` (default: "/")

#### view_logs
Read log files.
- **Parameters**: `log_file`, `lines` (default: 50)

### Group 5: Text Processing

#### grep_file
Search patterns in files.
- **Parameters**: `pattern`, `path`, `case_sensitive`, `recursive`

#### find_files
Find files by glob pattern.
- **Parameters**: `directory`, `pattern`, `file_type`

#### replace_in_file
Find and replace in files.
- **Parameters**: `file_path`, `search_text`, `replacement_text`, `use_regex`

### Group 6: Git Operations

#### git_clone
Clone repositories.
- **Parameters**: `repo_url`, `destination`, `branch`

#### git_status / git_commit / git_push / git_pull
Standard Git operations.
- **Parameters**: `repo_path` (default: "/app")

### Group 7: Archive Management

#### compress_files
Create archives.
- **Parameters**: `source_path`, `output_filename`, `format` (zip/tar.gz)

#### extract_archive
Extract archives.
- **Parameters**: `archive_path`, `destination`

### Group 8: Network

#### ping_host
Ping hosts.
- **Parameters**: `host`, `count`

#### check_port
Check port status.
- **Parameters**: `host`, `port`, `timeout`

#### get_ip_info
IP geolocation lookup.
- **Parameters**: `ip_or_domain` (optional)

### Group 9: Permissions & Environment

#### change_permissions
Chmod files.
- **Parameters**: `file_path`, `permissions`, `recursive`

#### get_env_variable / set_env_variable
Environment variable operations.
- **Parameters**: `variable_name`, `value`

### Group 10: Download Management

#### get_download_url
Generate public download URLs.
- **Parameters**: `filename`
- **Returns**: Download URL for /app/downloads files

## Deployment

### Docker

```dockerfile
FROM ubuntu:22.04
# ... full Dockerfile in repository
```

### Build & Run

```bash
docker build -t terminal-mcp .
docker run -p 7860:7860 terminal-mcp
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SPACE_HOST` | `localhost:7860` | Base URL for downloads |

## Process Tracking

The server maintains:
- `process_buffer` - Command output storage
- `process_status` - Running/finished/error states
- `process_read_index` - Output read positions
- `active_processes` - Active subprocess references

## Security Notes

- Commands run as root inside container
- No authentication on endpoints (use reverse proxy)
- File operations limited to container filesystem
- Temporary files auto-cleared on restart

## Dependencies

```
fastapi>=0.100.0
uvicorn>=0.23.0
mcp<2.0.0
requests>=2.31.0
beautifulsoup4>=4.12.0
psutil>=5.9.0
ddgs>=3.0.0
lxml>=4.9.0
cloudscraper>=1.2.0
```

## Version

- Server: 1.0.0
- MCP Protocol: <2.0.0
- Docker Image: Ubuntu 22.04
