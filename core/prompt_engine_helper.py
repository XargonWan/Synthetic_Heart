
async def free_memory_search(text: str, limit: int = 5) -> list[str]:
    """Execute a free-text memory search on DB (memories + ai_diary) without LLM."""
    if not text or not text.strip():
        return []

    # Simple keyword extraction
    words = [w.strip() for w in text.split() if len(w.strip()) > 3]
    stopwords = {'with', 'that', 'this', 'from', 'have', 'what', 'when', 'where', 'your', 'about', 'just', 'like', 'want', 'know', 'think'}
    keywords = [w for w in words if w.lower() not in stopwords]
    # Unique keywords
    keywords = list(set(keywords))

    if not keywords:
        return []

    # Limit keywods to keeping query simple
    keywords = keywords[:4]

    snippets = []
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Query memories
                conditions = []
                params = []
                for kw in keywords:
                    conditions.append("content LIKE %s")
                    params.append(f"%{kw}%")
                
                where = " OR ".join(conditions)
                
                # Search memories table
                q1 = f"SELECT content FROM memories WHERE {where} ORDER BY timestamp DESC LIMIT %s"
                await cur.execute(q1, params + [limit])
                rows1 = await cur.fetchall()
                for r in rows1:
                    snippets.append(str(r[0]))
                
                # Search ai_diary table
                if len(snippets) < limit:
                    remaining = limit - len(snippets)
                    q2 = f"SELECT content FROM ai_diary WHERE {where} ORDER BY timestamp DESC LIMIT %s"
                    await cur.execute(q2, params + [remaining])
                    rows2 = await cur.fetchall()
                    for r in rows2:
                        snippets.append(str(r[0]))

                # Search chat_history_cache table
                if len(snippets) < limit:
                    remaining = limit - len(snippets)
                    q3 = f"SELECT message_text FROM chat_history_cache WHERE {where.replace('content', 'message_text')} ORDER BY timestamp DESC LIMIT %s"
                    await cur.execute(q3, params + [remaining])
                    rows3 = await cur.fetchall()
                    for r in rows3:
                        snippets.append(str(r[0]))
                        
    except Exception as e:
        log_debug(f"[prompt_engine] free_memory_search warning: {e}")
        
    return snippets[:limit]
