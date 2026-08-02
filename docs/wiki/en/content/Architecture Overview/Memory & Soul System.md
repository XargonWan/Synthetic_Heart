# Memory & Soul System

<cite>
**Referenced Files in This Document**
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [models.py](file://core/soul/models.py)
- [schemas.py](file://core/soul/schemas.py)
- [strategies.py](file://core/soul/strategies.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [observability.py](file://core/soul/observability.py)
- [soul_plugin.py](file://plugins/soul_plugin/soul_plugin.py)
- [memory_search.py](file://plugins/memory_search/memory_search.py)
- [ai_diary_personal_memory.rst](file://docs/ai_diary_personal_memory.rst)
- [memory_search.rst](file://docs/memory_search.rst)
- [memory_search_and_management.rst](file://docs/memory_search_and_management.rst)
- [emotion_engine.rst](file://docs/emotion_engine.rst)
- [bootstrap_soul_postgres.sh](file://scripts/bootstrap_soul_postgres.sh)
- [sql/soul_memory_postgres.sql](file://scripts/sql/soul_memory_postgres.sql)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)
10. [Appendices](#appendices)

## Introduction
This document explains the architecture and implementation of Synthetic Heart’s Memory and Soul System. It covers the soul compiler, emotion engine, memory repository, semantic search, consolidation strategies, temporal context management, embedding system, and the compilation process for soul expressions. It also clarifies how soul scripts, emotion states, and personality persistence interact to form a coherent, evolving agent identity.

## Project Structure
The Memory & Soul System is implemented primarily under core/soul with supporting plugins and documentation:
- core/soul: Core runtime components (compiler, emotion engine, repository, embedder, models, schemas, strategies, time resolution, observability)
- plugins/soul_plugin: Integration hook that wires soul capabilities into the agent lifecycle
- plugins/memory_search: Semantic memory retrieval plugin exposing search APIs
- docs: Conceptual guides for memory, emotion engine, and memory search
- scripts: Database bootstrap and SQL schema for Postgres-backed memory storage

```mermaid
graph TB
subgraph "Soul Runtime"
C["compiler.py"]
E["emotion_engine.py"]
R["repository.py"]
F["fastembed_embedder.py"]
M["models.py"]
S["schemas.py"]
T["time_resolution.py"]
O["observability.py"]
STR["strategies.py"]
end
subgraph "Plugins"
SP["soul_plugin.py"]
MS["memory_search.py"]
end
subgraph "Docs"
D1["ai_diary_personal_memory.rst"]
D2["memory_search.rst"]
D3["memory_search_and_management.rst"]
D4["emotion_engine.rst"]
end
subgraph "Data"
DB["Postgres (soul_memory schema)"]
end
SP --> C
SP --> E
SP --> R
MS --> R
MS --> F
C --> M
C --> S
E --> M
R --> F
R --> T
R --> STR
R --> DB
C --> O
E --> O
R --> O
```

**Diagram sources**
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [models.py](file://core/soul/models.py)
- [schemas.py](file://core/soul/schemas.py)
- [strategies.py](file://core/soul/strategies.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [observability.py](file://core/soul/observability.py)
- [soul_plugin.py](file://plugins/soul_plugin/soul_plugin.py)
- [memory_search.py](file://plugins/memory_search/memory_search.py)
- [bootstrap_soul_postgres.sh](file://scripts/bootstrap_soul_postgres.sh)
- [sql/soul_memory_postgres.sql](file://scripts/sql/soul_memory_postgres.sql)

**Section sources**
- [ai_diary_personal_memory.rst](file://docs/ai_diary_personal_memory.rst)
- [memory_search.rst](file://docs/memory_search.rst)
- [memory_search_and_management.rst](file://docs/memory_search_and_management.rst)
- [emotion_engine.rst](file://docs/emotion_engine.rst)

## Core Components
- Soul Compiler: Translates soul script expressions into executable operations and state updates. It validates syntax, resolves references, and emits structured actions consumed by the emotion engine and repository.
- Emotion Engine: Maintains and evolves emotion states over time. It reacts to events, applies personality-driven rules, and persists emotional context as part of the persona.
- Memory Repository: Provides CRUD and query interfaces for episodic and semantic memories. It coordinates embeddings, indexing, and consolidation pipelines.
- Embedding System: Converts textual content into vector representations using a fast local embedder for efficient similarity search.
- Temporal Context Manager: Normalizes timestamps and contextual windows to support time-aware retrieval and consolidation.
- Strategies: Pluggable policies for memory retention, decay, clustering, and consolidation.
- Observability: Metrics, tracing, and logging hooks for debugging and performance tuning.

**Section sources**
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [strategies.py](file://core/soul/strategies.py)
- [observability.py](file://core/soul/observability.py)

## Architecture Overview
The Memory & Soul System integrates at the agent layer via a plugin. The compiler transforms soul scripts into structured operations; the emotion engine updates personality-emotion state; the repository stores and indexes memories; and semantic search exposes retrieval APIs.

```mermaid
sequenceDiagram
participant Agent as "Agent Core"
participant Plugin as "soul_plugin.py"
participant Compiler as "compiler.py"
participant Emotion as "emotion_engine.py"
participant Repo as "repository.py"
participant Embedder as "fastembed_embedder.py"
participant DB as "Postgres"
Agent->>Plugin : "Initialize soul subsystem"
Plugin->>Compiler : "Compile soul expression"
Compiler-->>Plugin : "Compiled action graph"
Plugin->>Emotion : "Apply emotion transitions"
Emotion-->>Plugin : "Updated emotion state"
Plugin->>Repo : "Persist memory event"
Repo->>Embedder : "Generate embeddings"
Embedder-->>Repo : "Vectors"
Repo->>DB : "Write records + vectors"
Agent->>Plugin : "Query semantic memory"
Plugin->>Repo : "Search(query, filters)"
Repo->>Embedder : "Embed query"
Repo->>DB : "Similarity scan"
DB-->>Repo : "Top-k results"
Repo-->>Plugin : "Ranked memories"
Plugin-->>Agent : "Results + metadata"
```

**Diagram sources**
- [soul_plugin.py](file://plugins/soul_plugin/soul_plugin.py)
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [sql/soul_memory_postgres.sql](file://scripts/sql/soul_memory_postgres.sql)

## Detailed Component Analysis

### Soul Compiler
The compiler parses and validates soul expressions, resolves variables and references, and produces an executable plan. It ensures type safety and consistency before emitting actions for downstream systems.

```mermaid
flowchart TD
Start(["Compile Entry"]) --> Parse["Parse Expression"]
Parse --> Validate{"Valid Syntax?"}
Validate --> |No| Error["Return Validation Error"]
Validate --> |Yes| Resolve["Resolve References"]
Resolve --> TypeCheck["Type Check & Constraints"]
TypeCheck --> Emit["Emit Action Plan"]
Emit --> End(["Compiled Plan"])
Error --> End
```

**Diagram sources**
- [compiler.py](file://core/soul/compiler.py)
- [schemas.py](file://core/soul/schemas.py)
- [models.py](file://core/soul/models.py)

**Section sources**
- [compiler.py](file://core/soul/compiler.py)
- [schemas.py](file://core/soul/schemas.py)
- [models.py](file://core/soul/models.py)

### Emotion Engine
The emotion engine maintains a dynamic affective state aligned with personality traits. It processes incoming signals, applies transition rules, and persists emotional context to influence future behavior and responses.

```mermaid
classDiagram
class EmotionEngine {
+update(signal) void
+get_state() dict
+apply_personality_rules(rules) void
+persist_state() void
}
class Models {
<<data>>
}
class Observability {
+trace_event(name, payload) void
+metrics_increment(key, value) void
}
EmotionEngine --> Models : "reads/writes"
EmotionEngine --> Observability : "logs/metrics"
```

**Diagram sources**
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [models.py](file://core/soul/models.py)
- [observability.py](file://core/soul/observability.py)

**Section sources**
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [observability.py](file://core/soul/observability.py)

### Memory Repository
The repository abstracts storage, retrieval, and indexing of memories. It orchestrates embedding generation, similarity search, temporal filtering, and consolidation workflows.

```mermaid
classDiagram
class Repository {
+create(memory) id
+read(id) Memory
+search(query, filters) Memory[]
+consolidate(strategy) void
+delete(id) bool
}
class FastEmbedder {
+embed(text) Vector
}
class TimeResolution {
+normalize(ts) Timestamp
+window(start, end) Filter
}
class Strategies {
+decay(memories) Memory[]
+cluster(memories) Cluster[]
}
class Models {
<<data>>
}
class Observability {
+trace_event(name, payload) void
}
Repository --> FastEmbedder : "embeddings"
Repository --> TimeResolution : "temporal filters"
Repository --> Strategies : "consolidation"
Repository --> Models : "schema mapping"
Repository --> Observability : "telemetry"
```

**Diagram sources**
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [strategies.py](file://core/soul/strategies.py)
- [models.py](file://core/soul/models.py)
- [observability.py](file://core/soul/observability.py)

**Section sources**
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [strategies.py](file://core/soul/strategies.py)
- [models.py](file://core/soul/models.py)
- [observability.py](file://core/soul/observability.py)

### Semantic Search Capabilities
Semantic search converts natural language queries into embeddings and performs similarity scans against stored memory vectors. Filters can include temporal windows, tags, and entity scopes.

```mermaid
sequenceDiagram
participant Client as "Client"
participant Search as "memory_search.py"
participant Repo as "repository.py"
participant Embedder as "fastembed_embedder.py"
participant DB as "Postgres"
Client->>Search : "semantic_search(query, filters)"
Search->>Repo : "search(query, filters)"
Repo->>Embedder : "embed(query)"
Embedder-->>Repo : "query_vector"
Repo->>DB : "vector similarity scan"
DB-->>Repo : "top-k ids"
Repo-->>Search : "ranked memories"
Search-->>Client : "results"
```

**Diagram sources**
- [memory_search.py](file://plugins/memory_search/memory_search.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [sql/soul_memory_postgres.sql](file://scripts/sql/soul_memory_postgres.sql)

**Section sources**
- [memory_search.py](file://plugins/memory_search/memory_search.py)
- [memory_search.rst](file://docs/memory_search.rst)
- [memory_search_and_management.rst](file://docs/memory_search_and_management.rst)

### Memory Consolidation Strategies
Consolidation reduces noise, merges related memories, and promotes long-term retention based on importance and recency. Strategies are pluggable and can be tuned per domain.

```mermaid
flowchart TD
Start(["Consolidation Trigger"]) --> Collect["Collect Candidate Memories"]
Collect --> Decay["Apply Decay Rules"]
Decay --> Cluster["Cluster Similar Items"]
Cluster --> Merge{"Merge Threshold Met?"}
Merge --> |Yes| Summarize["Summarize / Abstract"]
Merge --> |No| Keep["Keep As Is"]
Summarize --> Persist["Persist Consolidated Records"]
Keep --> Persist
Persist --> End(["Done"])
```

**Diagram sources**
- [strategies.py](file://core/soul/strategies.py)
- [repository.py](file://core/soul/repository.py)

**Section sources**
- [strategies.py](file://core/soul/strategies.py)
- [repository.py](file://core/soul/repository.py)

### Temporal Context Management
Temporal context normalizes timestamps and supports windowed queries. It enables time-aware retrieval and influences consolidation decisions.

```mermaid
classDiagram
class TimeResolution {
+normalize(timestamp) Timestamp
+to_window(start, end) Filter
+resolve_relative(rel_str) Timestamp
}
class Repository {
+search(query, filters) Memory[]
}
TimeResolution <.. Repository : "used by"
```

**Diagram sources**
- [time_resolution.py](file://core/soul/time_resolution.py)
- [repository.py](file://core/soul/repository.py)

**Section sources**
- [time_resolution.py](file://core/soul/time_resolution.py)
- [repository.py](file://core/soul/repository.py)

### Relationship Between Soul Scripts, Emotion States, and Personality Persistence
Soul scripts define behavioral intentions and memory triggers. The compiler translates them into actions that update emotion states and persist relevant memories. Personality persistence ensures continuity across sessions by storing emotion baselines and trait modifiers.

```mermaid
sequenceDiagram
participant Script as "Soul Script"
participant Compiler as "compiler.py"
participant Emotion as "emotion_engine.py"
participant Repo as "repository.py"
participant Persist as "Personality Store"
Script->>Compiler : "Expression"
Compiler-->>Script : "Action Plan"
Script->>Emotion : "Trigger emotion update"
Emotion-->>Script : "New emotion state"
Script->>Repo : "Record memory event"
Repo-->>Script : "ID + metadata"
Script->>Persist : "Save personality baseline"
```

**Diagram sources**
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)

**Section sources**
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)

### Data Flow: Creation, Storage, Retrieval, and Consolidation
Memories flow from creation through embedding, storage, retrieval, and periodic consolidation.

```mermaid
flowchart TD
A["Create Memory Event"] --> B["Validate & Normalize"]
B --> C["Generate Embedding"]
C --> D["Store in Repository"]
D --> E["Index for Search"]
E --> F["Expose via Semantic Search"]
D --> G["Periodic Consolidation"]
G --> H["Decay & Cluster"]
H --> I["Summarize & Persist"]
I --> J["Update Index"]
```

**Diagram sources**
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [strategies.py](file://core/soul/strategies.py)

**Section sources**
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [strategies.py](file://core/soul/strategies.py)

## Dependency Analysis
The soul subsystem has clear boundaries:
- Plugins depend on core modules for functionality
- Repository depends on embedder, time resolution, and strategies
- Compiler depends on models and schemas
- Observability is cross-cutting

```mermaid
graph LR
SP["soul_plugin.py"] --> C["compiler.py"]
SP --> E["emotion_engine.py"]
SP --> R["repository.py"]
MS["memory_search.py"] --> R
R --> F["fastembed_embedder.py"]
R --> T["time_resolution.py"]
R --> STR["strategies.py"]
C --> M["models.py"]
C --> S["schemas.py"]
E --> M
R --> O["observability.py"]
C --> O
E --> O
```

**Diagram sources**
- [soul_plugin.py](file://plugins/soul_plugin/soul_plugin.py)
- [memory_search.py](file://plugins/memory_search/memory_search.py)
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [strategies.py](file://core/soul/strategies.py)
- [models.py](file://core/soul/models.py)
- [schemas.py](file://core/soul/schemas.py)
- [observability.py](file://core/soul/observability.py)

**Section sources**
- [soul_plugin.py](file://plugins/soul_plugin/soul_plugin.py)
- [memory_search.py](file://plugins/memory_search/memory_search.py)
- [compiler.py](file://core/soul/compiler.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)
- [repository.py](file://core/soul/repository.py)
- [fastembed_embedder.py](file://core/soul/fastembed_embedder.py)
- [time_resolution.py](file://core/soul/time_resolution.py)
- [strategies.py](file://core/soul/strategies.py)
- [models.py](file://core/soul/models.py)
- [schemas.py](file://core/soul/schemas.py)
- [observability.py](file://core/soul/observability.py)

## Performance Considerations
- Use batched embedding generation for high-throughput ingestion.
- Configure index size and top-k thresholds to balance latency and recall.
- Apply decay and clustering aggressively during off-peak hours.
- Cache frequent query embeddings and result sets where appropriate.
- Monitor observability metrics to identify bottlenecks in embedding or search paths.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and diagnostics:
- Compilation errors: Inspect validation logs and schema mismatches.
- Search failures: Verify embedding model availability and vector index health.
- Consolidation stalls: Check strategy configuration and database locks.
- Emotion state drift: Review personality rule updates and persistence integrity.

Use observability hooks to trace events and collect metrics for each stage.

**Section sources**
- [observability.py](file://core/soul/observability.py)
- [compiler.py](file://core/soul/compiler.py)
- [repository.py](file://core/soul/repository.py)
- [emotion_engine.py](file://core/soul/emotion_engine.py)

## Conclusion
The Memory & Soul System provides a robust foundation for persistent personality, emotion dynamics, and semantic memory. By separating concerns across compiler, emotion engine, repository, and embedder, it achieves modularity, scalability, and extensibility. Proper configuration of strategies and temporal context ensures meaningful long-term behavior and retrieval quality.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices

### Database Setup and Schema
Bootstrap the Postgres schema for soul memory and ensure vector indexing is enabled.

**Section sources**
- [bootstrap_soul_postgres.sh](file://scripts/bootstrap_soul_postgres.sh)
- [sql/soul_memory_postgres.sql](file://scripts/sql/soul_memory_postgres.sql)

### Documentation References
Conceptual guides provide additional context on memory, emotion engine, and search usage.

**Section sources**
- [ai_diary_personal_memory.rst](file://docs/ai_diary_personal_memory.rst)
- [memory_search.rst](file://docs/memory_search.rst)
- [memory_search_and_management.rst](file://docs/memory_search_and_management.rst)
- [emotion_engine.rst](file://docs/emotion_engine.rst)