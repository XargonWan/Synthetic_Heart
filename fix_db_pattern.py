#!/usr/bin/env python3
"""
Script to convert all occurrences of the old get_conn() pattern to the new get_conn_ctx() pattern.
This fixes the database connection pool exhaustion issue.
"""

import re
import os
import sys
from pathlib import Path

# Files that need to be converted
PLUGIN_FILES = [
    "plugins/blocklist.py",
    "plugins/message_map.py",
    "plugins/recent_chats.py",
    "plugins/ai_diary.py",
    "plugins/chat_link.py",
]

def has_async_def(content: str) -> bool:
    """Check if content contains async function definitions."""
    return "async def" in content

def needs_import_update(content: str) -> bool:
    """Check if file imports get_conn but not get_conn_ctx."""
    has_get_conn_import = "from core.db import" in content and "get_conn" in content
    has_get_conn_ctx_import = "get_conn_ctx" in content
    return has_get_conn_import and not has_get_conn_ctx_import

def update_imports(content: str) -> str:
    """Update imports to include get_conn_ctx."""
    # Update the import line
    content = re.sub(
        r'from core\.db import ([^\n]*get_conn[^\n]*)',
        lambda m: update_import_line(m.group(1)),
        content
    )
    return content

def update_import_line(import_list: str) -> str:
    """Update a single import line to include get_conn_ctx."""
    imports = [i.strip() for i in import_list.split(',')]
    
    # Remove 'get_conn' if present
    imports = [i for i in imports if i != 'get_conn']
    
    # Add 'get_conn_ctx' if not present
    if 'get_conn_ctx' not in imports:
        imports.append('get_conn_ctx')
    
    return f"from core.db import {', '.join(imports)}"

def convert_async_function_pattern(content: str) -> str:
    """
    Convert the pattern:
        conn = await get_conn()
        try:
            async with conn.cursor...
        finally:
            conn.close()
    
    To:
        async with get_conn_ctx() as conn:
            async with conn.cursor...
    """
    
    # Pattern 1: Handle basic try-finally with get_conn()
    pattern1 = r'(\s+)conn = await get_conn\(\)\s*\n(\s+)try:\s*\n((?:(?!\n\s{0,8}(?:except|finally|elif|else))\s+.+\n)*?)(\s+)finally:\s*\n(\s+)conn\.close\(\)'
    
    def replacement1(m):
        indent = m.group(1)
        try_content = m.group(3)
        # The try content should be dedented if there's extra indentation
        return f"{indent}async with get_conn_ctx() as conn:\n{try_content}"
    
    content = re.sub(pattern1, replacement1, content)
    
    # Pattern 2: Handle cases without try-except (just using get_conn directly)
    pattern2 = r'(\s+)conn = await get_conn\(\)\s*\n(\s+)try:\s*\n((?:[^\n]*\n)*?)(\s+)except Exception'
    
    # This is more complex - we need to be careful
    # For now, skip this pattern as it's less common
    
    return content

def convert_file(filepath: str) -> bool:
    """Convert a single file to use get_conn_ctx. Returns True if file was modified."""
    print(f"Processing {filepath}...")
    
    if not os.path.exists(filepath):
        print(f"  ❌ File not found: {filepath}")
        return False
    
    with open(filepath, 'r') as f:
        original_content = f.read()
    
    if not has_async_def(original_content):
        print(f"  ⏭️  No async functions found")
        return False
    
    if 'get_conn_ctx' in original_content:
        print(f"  ⏭️  Already uses get_conn_ctx")
        return False
    
    # Step 1: Update imports
    content = update_imports(original_content)
    
    # Step 2: Convert the pattern manually with a more sophisticated approach
    # We need to handle this carefully due to the complexity
    
    if "conn = await get_conn()" in content:
        # Mark for manual conversion
        print(f"  ⚠️  Requires manual conversion (complex patterns)")
        return False
    
    if content != original_content:
        with open(filepath, 'w') as f:
            f.write(content)
        print(f"  ✅ Updated imports")
        return True
    
    return False

if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    
    modified = 0
    for filepath in PLUGIN_FILES:
        if convert_file(filepath):
            modified += 1
    
    print(f"\n{modified} files modified")
    print("\nNote: Manual pattern conversion still needed for plugin files.")
    print("Use replace_string_in_file tool to update the actual patterns.")
