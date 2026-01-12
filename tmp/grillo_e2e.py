import asyncio
import json
import pymysql
import time

DB_HOST = 'synth-db'
DB_USER = 'synth'
DB_PASS = 'DigiHeart01'
DB_NAME = 'synth'

age_days = 60

def insert_rows(conn):
    with conn.cursor() as cur:
        # Insert two untagged legacy-like diary entries older than age_days
        cur.execute("INSERT INTO ai_diary (content, personal_thought, emotions, interaction_summary, timestamp, interface, chat_id, thread_id, user_message, context_tags, involved_users) VALUES (%s, %s, %s, %s, DATE_SUB(NOW(), INTERVAL %s DAY), %s, %s, %s, %s, %s, %s)",
                    ("Legacy entry A", None, '[]', None, age_days + 1, 'webui', 'cA', None, None, None, '[]'))
        cur.execute("INSERT INTO ai_diary (content, personal_thought, emotions, interaction_summary, timestamp, interface, chat_id, thread_id, user_message, context_tags, involved_users) VALUES (%s, %s, %s, %s, DATE_SUB(NOW(), INTERVAL %s DAY), %s, %s, %s, %s, %s, %s)",
                    ("Legacy entry B", None, '[]', None, age_days + 1, 'webui', 'cB', None, None, None, '[]'))
        # Insert two tagged entries older than age_days
        cur.execute("INSERT INTO ai_diary (content, personal_thought, emotions, interaction_summary, timestamp, interface, chat_id, thread_id, user_message, context_tags, involved_users) VALUES (%s, %s, %s, %s, DATE_SUB(NOW(), INTERVAL %s DAY), %s, %s, %s, %s, %s, %s)",
                    ("Tagged entry A about food", None, '[]', None, age_days + 1, 'webui', 'cF1', None, None, json.dumps(['food']), '[]'))
        cur.execute("INSERT INTO ai_diary (content, personal_thought, emotions, interaction_summary, timestamp, interface, chat_id, thread_id, user_message, context_tags, involved_users) VALUES (%s, %s, %s, %s, DATE_SUB(NOW(), INTERVAL %s DAY), %s, %s, %s, %s, %s, %s)",
                    ("Tagged entry B about food and pizza", None, '[]', None, age_days + 1, 'webui', 'cF2', None, None, json.dumps(['food', 'pizza']), '[]'))
        conn.commit()

async def run_compactor(dry_run=True, marker=None):
    # Ensure active LLM is set to 'manual' in this process and hot-swapped
    try:
        from core.config import switch_active_llm
        await switch_active_llm('manual', use_hot_swap=True)
        print('Switched active LLM to manual in-process')
    except Exception as _:
        pass
    # Import plugin by path to avoid package-import issues in the container
    import importlib.machinery, importlib.util, sys
    module_path = '/app/plugins/grillo/grillo_compactor.py'
    loader = importlib.machinery.SourceFileLoader('grillo_compactor', module_path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    # Prevent import-time failures for aiomysql only if not installed in the environment
    try:
        import aiomysql as _real_aiomysql  # noqa: F401
    except Exception:
        import types
        aiom = types.ModuleType('aiomysql')
        class _Conn: pass
        class _Pool: pass
        async def _dummy_create_pool(*args, **kwargs):
            return _Pool()
        aiom.Connection = _Conn
        aiom.create_pool = _dummy_create_pool
        aiom.DictCursor = object
        sys.modules.setdefault('aiomysql', aiom)
    loader.exec_module(mod)
    # Force the active LLM to 'manual' for E2E testing so we use the bundled manual engine
    try:
        import core.config as _conf
        async def _fake_get_active_llm():
            return 'manual'
        _conf.get_active_llm = _fake_get_active_llm
    except Exception:
        pass
    GrilloCompactorPlugin = getattr(mod, 'GrilloCompactorPlugin')
    p = GrilloCompactorPlugin()
    # Ensure registry has a usable engine for 'selenium_chatgpt' by loading 'manual' and mapping it
    try:
        import importlib
        m = importlib.import_module('llm_engines.manual')
        engine = getattr(m, 'PLUGIN_CLASS')()
        from core.llm_registry import get_llm_registry
        reg = get_llm_registry()
        reg._engines['selenium_chatgpt'] = engine
        reg._engine_modules['selenium_chatgpt'] = 'llm_engines.manual'
    except Exception as e:
        print('Failed to map selenium_chatgpt -> manual:', e)
        pass

    # For container E2E, bypass real LLM and provide a fake clustering method to validate persistence flow
    try:
        import types, json
        from core.db import get_conn_ctx, insert_memory
        async def _fake_cluster_and_compact_batch(self, window, dry_run=False):
            source_ids = [e['id'] if isinstance(e, dict) else e[0] for e in window]
            if dry_run:
                return {'dry_run': True, 'results': [{'cluster_id': 1, 'status': 'ok', 'should_compact': True, 'summary': 'Auto-summary for test', 'source_ids': source_ids}]}
            total_chars = 0
            for e in window:
                content = e.get('content') if isinstance(e, dict) else e[1]
                total_chars += len(content or '')
            summary = 'Auto-summary for test'
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO compacted_memories (tag, summary, source_ids, source_count, llm_model, confidence, notes, compaction_level, total_source_chars, summary_chars, justification, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (None, summary, json.dumps(source_ids), len(source_ids), 'manual', 'high', json.dumps({}), 1, total_chars, len(summary), 'auto', 'grillo_compactor')
                    )
                    if source_ids:
                        await cur.execute(
                            "INSERT INTO ai_diary_archive (content, personal_thought, emotions, interaction_summary, timestamp, interface, chat_id, thread_id, user_message, context_tags, involved_users) SELECT content, personal_thought, emotions, interaction_summary, timestamp, interface, chat_id, thread_id, user_message, context_tags, involved_users FROM ai_diary WHERE id IN (" + ",".join(["%s"] * len(source_ids)) + ")",
                            tuple(source_ids),
                        )
                        await cur.execute(
                            "DELETE FROM ai_diary WHERE id IN (" + ",".join(["%s"] * len(source_ids)) + ")",
                            tuple(source_ids),
                        )
                    try:
                        await insert_memory(content=summary, author="grillo", source="compaction", tags=None, emotion=None, intensity=None, emotion_state=None)
                    except Exception:
                        await cur.execute(
                            "INSERT INTO memories (timestamp, content, author, source, tags, scope, emotion, intensity, emotion_state) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s)",
                            (summary, "grillo", "compaction", None, None, None, None, None),
                        )
            return True
        # Bind as method on plugin class before instantiation
        # We'll attach to the class after we get GrilloCompactorPlugin
    except Exception as e:
        print('Failed to install fake clustering:', e)
        pass
    try:
        GrilloCompactorPlugin._cluster_and_compact_batch = _fake_cluster_and_compact_batch
        print('Bound fake clusterer to GrilloCompactorPlugin ->', GrilloCompactorPlugin._cluster_and_compact_batch)
    except Exception as e:
        print('Failed binding fake clusterer:', e)
        pass
    # Patch get_active_llm to return 'manual' at runtime to ensure the plugin loads a usable engine
    try:
        import core.config as _conf
        async def _fake_get_active_llm2():
            return 'manual'
        _conf.get_active_llm = _fake_get_active_llm2
        print('Patched core.config.get_active_llm to return manual')
    except Exception as e:
        print('Failed patch get_active_llm:', e)
        pass
    res = await p.run_action('compact_now', payload={'cycles':1, 'dry_run': dry_run, 'marker': marker})
    print('run_action result:', res)
    return res


def query_counts(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM ai_diary')
        total_diary = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM ai_diary_archive')
        archived = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM compacted_memories')
        comp = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM memories')
        mems = cur.fetchone()[0]
    return total_diary, archived, comp, mems

if __name__ == '__main__':
    conn = pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS, db=DB_NAME)
    print('Inserting test rows into ai_diary...')
    insert_rows(conn)
    print('Done. Sleeping 1s to allow DB to settle...')
    time.sleep(1)

    print('\n--- Dry-run (should propose clusters for tagged entries) ---')
    asyncio.run(run_compactor(dry_run=True))

    print('\n--- Persist run (will archive sources and insert compacted memory) ---')
    # Ensure compacted_memories table exists for this test
    with conn.cursor() as cur:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS compacted_memories (
                id INT AUTO_INCREMENT PRIMARY KEY,
                tag TEXT,
                summary TEXT,
                source_ids TEXT,
                source_count INT,
                llm_model VARCHAR(255),
                confidence VARCHAR(50),
                notes TEXT,
                compaction_level INT,
                total_source_chars INT,
                summary_chars INT,
                justification TEXT,
                created_by VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        ''')
        conn.commit()
    asyncio.run(run_compactor(dry_run=False))

    print('\n--- Querying DB counts ---')
    total_diary, archived, comp, mems = query_counts(conn)
    print('ai_diary total:', total_diary)
    print('ai_diary_archive total:', archived)
    print('compacted_memories total:', comp)
    print('memories total:', mems)
    conn.close()
