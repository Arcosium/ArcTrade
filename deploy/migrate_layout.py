"""Move the source tree, preserving runtime references. Explicit --apply only."""
import argparse
import json
import shutil
import time
from pathlib import Path


def migrate(projects, backup):
    old=projects/'QuantInSight';new=projects/'ArcTrade'
    if not new.is_symlink() or new.resolve()!=old/'integrations/timefolio':
        raise RuntimeError('Unexpected ArcTrade compatibility link; refusing overwrite')
    if not (old/'autofolio/app.py').is_file():raise RuntimeError('Source tree unavailable')
    backup.mkdir(parents=True,exist_ok=False)
    (backup/'layout.json').write_text(json.dumps({'old_arctrade_link':str(new.readlink()),'autofolio_link':str((projects/'Autofolio').readlink())}))
    # Atomic directory rename, no copy of datasets or Git history.
    new.unlink();old.rename(new);old.mkdir()
    placeholder=new/'deploy/quantinsight_placeholder'
    shutil.copytree(placeholder,old,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    # Existing collectors and sealed recipes retain their exact paths.
    for entry in new.iterdir():
        if entry.name.startswith('.') or entry.name in {'server','static','README.md','requirements.txt'}:continue
        (old/entry.name).symlink_to(entry,target_is_directory=entry.is_dir())
    (projects/'Autofolio').unlink();(projects/'Autofolio').symlink_to(new,target_is_directory=True)
    (old/'.gitignore').write_text('/*\n!/README.md\n!/requirements.txt\n!/server/\nserver/__pycache__/\n')
    return new


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');a=p.parse_args()
    home=Path.home()
    if not a.apply:print('QuantInSight source → ArcTrade; QuantInSight → blank site with legacy path links')
    else:print(migrate(home/'projects',home/'vault/backups'/('arctrade-layout-'+str(time.time_ns()))))
