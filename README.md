# Terminal MCP Server

A powerful Model Context Protocol (MCP) server that provides AI assistants with full terminal capabilities. Run bash commands, manage files, search the web, interact with Git, and more - all through the MCP protocol.

## Features

### Command Execution
- `execute_command` - Run any bash command synchronously
- `run_python_code` - Execute Python code directly
- `kill_process` - Terminate running processes
- `stream_output` - Run long-running commands in background
- `get_process_output` - Read output from background processes

### File Management
- `read_file` - Read file contents with line range support
- `write_file` - Create or overwrite files
- `delete_file` - Delete files and directories
- `move_file` - Move or rename files
- `copy_file` - Copy files and directories
- `list_directory` - List directory contents
- `create_directory` - Create new directories

### Web & Internet
- `fetch_url` - Fetch webpage content (HTML or plain text)
- `download_file_from_url` - Download files to /app/downloads
- `search_web` - Search the web using DuckDuckGo
- `http_request` - Send custom HTTP requests (GET, POST, PUT, DELETE, PATCH)

### Package & System
- `install_package` - Install packages via pip, apt, or npm
- `get_system_info` - Get CPU, RAM, disk usage info
- `list_processes` - List running processes
- `check_disk_space` - Check available disk space
- `view_logs` - Read system log files

### Text Processing
- `grep_file` - Search for patterns in files
- `find_files` - Find files by name pattern
- `replace_in_file` - Find and replace text in files

### Git Operations
- `git_clone` - Clone repositories
- `git_status` - Check repository status
- `git_commit` - Create commits
- `git_push` - Push to remote
- `git_pull` - Pull from remote

### Archive Management
- `compress_files` - Create zip or tar.gz archives
- `extract_archive` - Extract zip, tar.gz, tar.bz2, tar files

### Network Tools
- `ping_host` - Ping a host
- `check_port` - Check if a port is open
- `get_ip_info` - Get IP geolocation info

### Permissions & Environment
- `change_permissions` - chmod files and directories
- `get_env_variable` - Read environment variables
- `set_env_variable` - Set environment variables

### Download Management
- `get_download_url` - Generate public download URLs for files in /app/downloads

## Quick Start

### Running with Docker

```bash
# Build the image
docker build -t terminal-mcp .

# Run the container
docker run -p 7860:7860 terminal-mcp
```

### Running with Docker Compose

```bash
docker-compose up -d
```

### Accessing the Server

- **API Root**: `http://localhost:7860/`
- **SSE Endpoint**: `http://localhost:7860/sse`
- **Download Files**: `http://localhost:7860/download/{filename}`

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SPACE_HOST` | Base URL for download links | `localhost:7860` |

### Directories

| Directory | Purpose |
|-----------|---------|
| `/app/downloads` | Store downloadable files |
| `/app/temp` | Temporary files (auto-cleared) |

## System Requirements

- Ubuntu 22.04
- Python 3.10+
- Node.js 20
- Docker (for containerized deployment)

## Built with

- **FastAPI** - Web framework
- **MCP SDK** - Model Context Protocol server
- **DuckDuckGo Search** - Web search
- **BeautifulSoup4** - HTML parsing
- **psutil** - System monitoring

## License

MIT License
