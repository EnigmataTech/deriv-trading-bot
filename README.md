# MCP Server Backend

A FastMCP-based Model Context Protocol server that provides authenticated note management tools.

## Features

- **MCP Tools**: 
  - `get_my_notes()`: Retrieve all notes for authenticated user
  - `add_note(content)`: Create new notes for authenticated user
- **Authentication**: Stytch JWT-based authentication with Bearer tokens
- **Database**: SQLite database with SQLAlchemy ORM
- **Transport**: HTTP transport on port 8000
- **CORS**: Enabled for cross-origin requests
- **OAuth 2.0**: Discovery endpoint for authentication metadata

## Setup

1. Install dependencies using uv:
```bash
uv sync
```

2. Create a `.env` file with your Stytch configuration:
```env
STYTCH_DOMAIN=https://test.stytch.com
STYTCH_PROJECT_ID=your_project_id
```

3. The SQLite database will be created automatically when you run the server.

## Running the Server

```bash
python main.py
```

The server will start on `http://127.0.0.1:8000`

## MCP Integration

### For n8n MCP Client Node:
- **Server URL**: `http://127.0.0.1:8000`
- **Transport**: HTTP
- **Authentication**: Bearer token (Stytch JWT)
- **OAuth Discovery**: `http://127.0.0.1:8000/.well-known/oauth-protected-resource`

### Available Tools:
- `get_my_notes()`: Returns all notes for the authenticated user
- `add_note(content: str)`: Adds a new note with the specified content

## Database Schema

```sql
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    content TEXT NOT NULL
);
```

## Authentication Flow

1. User authenticates via Stytch (frontend or direct API)
2. Stytch returns JWT token
3. MCP client includes JWT in Authorization header: `Bearer <token>`
4. Server validates JWT using Stytch JWKS endpoint
5. User ID extracted from JWT claims for database operations

## Development

The server uses FastMCP framework which provides:
- Automatic MCP protocol handling
- Built-in authentication middleware
- HTTP transport support
- Tool registration and validation