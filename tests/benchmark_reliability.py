"""Reproducible benchmarks on a backup and disposable files, never on originals."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import database


def measure(fn,repeats=3):
    values=[]
    for _ in range(repeats):
        started=time.perf_counter();fn();values.append(round((time.perf_counter()-started)*1000,2))
    return values


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--database',type=Path);parser.add_argument('--files',type=int,default=500);args=parser.parse_args()
    scratch=Path(__file__).resolve().parents[1]/'.tag_gallery';scratch.mkdir(exist_ok=True)
    results={}
    with tempfile.TemporaryDirectory(prefix='audit-benchmark-',dir=scratch) as directory,ExitStack() as stack:
        root=Path(directory);data=root/'.tag_gallery';data.mkdir()
        for name,value in {'GALLERY_ROOT':root,'DATA_DIR':data,'THUMB_DIR':data/'thumbs','MODEL_DIR':data/'models','DB_PATH':data/'gallery.db'}.items():
            stack.enter_context(patch.object(database,name,value))
        if args.database:
            source=sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True)
            destination=sqlite3.connect(database.DB_PATH)
            source.backup(destination);source.close();destination.close()
        database.init_db()
        import app
        stack.enter_context(patch.object(app.thumbnail_cache,'prefetch'))
        stack.enter_context(patch.object(app,'mark_interactive'))
        conn=database.get_db()
        results['images']=conn.execute('SELECT COUNT(*) FROM images').fetchone()[0]
        results['links']=conn.execute('SELECT COUNT(*) FROM image_tags').fetchone()[0]
        kwargs=dict(include_tags='',exclude_tags='',folder='__ALL__',media_type='all',ai_status='all',q='',sort='natural_asc',page=1,page_size=60)
        from renamer import win_natural_key
        results['natural_uncached_fetch_sort_ms']=measure(lambda:sorted(conn.execute('SELECT id,rel_path FROM images').fetchall(),key=lambda row:win_natural_key(row['rel_path']))[:60])
        results['natural_full_api_cold_warm_ms']=measure(lambda:app.api_images(**kwargs))
        old_sql='''SELECT COUNT(*) FROM images i WHERE i.filename LIKE ? OR i.folder LIKE ? OR EXISTS(
            SELECT 1 FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=i.id AND (t.name LIKE ? OR t.zh_name LIKE ?))'''
        results['keyword_old_count_ms']=measure(lambda:conn.execute(old_sql,['%audit_no_matching_keyword%']*4).fetchone())
        results['keyword_new_full_api_ms']=measure(lambda:app.api_images(**{**kwargs,'q':'audit_no_matching_keyword','sort':'mtime_desc'}))
        for query in ('1girl','黑','audit_no_matching_keyword'):
            old=conn.execute(old_sql,[f'%{query}%']*4).fetchone()[0]
            new=app.api_images(**{**kwargs,'q':query,'sort':'mtime_desc'})['total']
            assert old==new,(query,old,new)
        results['recent_tags_full_api_cold_warm_ms']=measure(lambda:app.api_tags(category='recent',q='',limit=250))
        with conn:database.refresh_tag_counts(conn)
        results['no_dirty_tag_count_refresh_ms']=measure(lambda:database.refresh_tag_counts(conn))
        conn.close()
        # File-operation comparison uses the same files, names, and database.
        conn=database.get_db();records=[]
        for index in range(args.files):
            name=f'audit_{index}.png';(root/name).write_bytes(b'benchmark')
            cursor=conn.execute("INSERT INTO images(rel_path,filename,ext) VALUES(?,?,'.png')",(name,name))
            records.append({'id':cursor.lastrowid,'old_rel_path':name,'new_rel_path':f'new_audit_{index}.png'})
        conn.commit();conn.close()
        def baseline():
            for item in records:os.rename(root/item['old_rel_path'],root/('tmp_'+item['old_rel_path']))
            for item in records:os.rename(root/('tmp_'+item['old_rel_path']),root/item['new_rel_path'])
            c=database.get_db()
            with c:c.executemany('UPDATE images SET rel_path=?,filename=? WHERE id=?',[(item['new_rel_path'],item['new_rel_path'],item['id']) for item in records])
            c.close()
        results['files']=args.files
        results['old_two_phase_rename_ms']=measure(baseline,repeats=1)
        for item in records:os.rename(root/item['new_rel_path'],root/item['old_rel_path'])
        c=database.get_db()
        with c:c.executemany('UPDATE images SET rel_path=?,filename=? WHERE id=?',[(item['old_rel_path'],item['old_rel_path'],item['id']) for item in records])
        c.close()
        from file_operations import execute
        results['recoverable_rename_ms']=measure(lambda:execute(root,data,database.get_db,records),repeats=1)
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
