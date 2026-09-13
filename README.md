# AI Assistant

A persistent-chat AI assistant built on the Anthropic API.

## System architecture

```mermaid
flowchart LR
    subgraph Clients
        CLI[CLI client]
        WEB[Web UI - phase 3]
    end
    subgraph Backend[FastAPI backend]
        API[REST and SSE endpoints]
        SVC[Chat service]
        REPO[Repository layer]
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

Clients never talk to Anthropic directly. The API key lives only in the backend. The chat service is the only component that calls the model, and the repository layer is the only component that touches the database.

## Request flow for one message

```mermaid
sequenceDiagram
    participant C as Client
    participant A as FastAPI
    participant D as Database
    participant M as Anthropic API
    C->>A: POST /conversations/{id}/messages
    A->>D: save user message
    A->>D: load history + system prompt
    D-->>A: messages
    A->>M: messages.create stream
    M-->>A: text chunks
    A-->>C: stream chunks via SSE
    A->>D: save assistant message
    A-->>C: done
```

## Data model

```mermaid
erDiagram
    USERS ||--o{ CONVERSATIONS : owns
    CONVERSATIONS ||--o{ MESSAGES : contains
    USERS {
        uuid id PK
        string email
        datetime created_at
    }
    CONVERSATIONS {
        uuid id PK
        uuid user_id FK
        string title
        text system_prompt
        string model
        datetime created_at
        datetime updated_at
    }
    MESSAGES {
        uuid id PK
        uuid conversation_id FK
        string role
        text content
        int token_count
        datetime created_at
    }
```

## Roadmap

- Phase 1: CLI chat with persistent conversations (FastAPI + SQLite)
- Phase 2: Streaming responses, conversation management (list, rename, delete)
- Phase 3: Web UI
- Phase 4: Auth, multi-user, Postgres, deployment

---

**If it still shows as plain text after committing**, tell me exactly what you see — e.g. "grey box with the code inside" vs "the words `flowchart LR` in normal text" — and I'll know which of the two failure modes it is. The most common one is an accidental leading space or stray backtick before ` ```mermaid `.

Once the diagrams render, we pick Python vs TypeScript and start on the actual code.
