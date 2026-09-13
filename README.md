````markdown
# AI Assistant

A persistent-chat AI assistant built on the Anthropic API.

## System architecture

```mermaid
flowchart LR
    subgraph Clients
        CLI[CLI client]
        WEB[Web UI - phase 3]
    end

    subgraph Backend["FastAPI backend"]
        API[REST and SSE endpoints]
        SVC[Chat service - builds context, calls model]
        REPO[Repository layer - DB access]
    end

    subgraph Storage
        DB[(SQLite then Postgres)]
    end

    subgraph External
        ANTH[Anthropic API]
    end

    CLI -->|HTTP| API
    WEB -->|HTTP| API
    API --> SVC
    SVC --> REPO
    REPO --> DB
    SVC -->|messages + system prompt| ANTH
    ANTH -->|streamed response| SVC
```

Clients never talk to Anthropic directly. The API key lives only in the backend.

## Request flow for one message

```mermaid
sequence
