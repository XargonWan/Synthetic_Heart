import pymysql
conn = pymysql.connect(host='synth-db', user='synth', password='DigiHeart01', db='synth', cursorclass=pymysql.cursors.DictCursor)
try:
    cur = conn.cursor()
    age=30
    cur.execute('SELECT COUNT(*) AS cnt_total FROM memories WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)', (age,))
    print('count_older_30_days:', cur.fetchone()['cnt_total'])
    cur.execute("SELECT COUNT(*) AS cnt_tagged FROM memories WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY) AND tags IS NOT NULL AND tags <> '' AND tags <> '[]'", (age,))
    print('count_tagged_older_30_days:', cur.fetchone()['cnt_tagged'])
    cur.execute('SELECT id, tags, timestamp, CHAR_LENGTH(content) as content_len FROM memories WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY) ORDER BY timestamp ASC LIMIT 20', (age,))
    rows = cur.fetchall()
    print('\nsample_oldest_rows (up to 20):')
    for r in rows:
        print(r)
    cur.execute('SELECT COUNT(*) AS cnt_total_all FROM memories')
    print('\ncount_all_memories:', cur.fetchone()['cnt_total_all'])
    cur.execute("SELECT COUNT(*) AS cnt_tagged_all FROM memories WHERE tags IS NOT NULL AND tags <> '' AND tags <> '[]'")
    print('count_tagged_all:', cur.fetchone()['cnt_tagged_all'])
finally:
    conn.close()
