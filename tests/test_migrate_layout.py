from pathlib import Path
from deploy.migrate_layout import migrate


def test_rename_keeps_original_data_and_old_recipe_paths(tmp_path):
    projects=tmp_path/'projects';old=projects/'QuantInSight';old.mkdir(parents=True)
    for folder in ['autofolio','integrations/timefolio','deploy/quantinsight_placeholder/server']:(old/folder).mkdir(parents=True)
    (old/'autofolio/app.py').write_text('source')
    (old/'deploy/quantinsight_placeholder/server/app.py').write_text('blank')
    data=tmp_path/'private-data';data.mkdir();(old/'data').symlink_to(data)
    (projects/'ArcTrade').symlink_to(old/'integrations/timefolio');(projects/'Autofolio').symlink_to(old)
    source_inode=(old/'autofolio/app.py').stat().st_ino
    result=migrate(projects,tmp_path/'backup')
    assert result==projects/'ArcTrade' and not result.is_symlink()
    assert (old/'autofolio/app.py').read_text()=='source'
    assert (old/'autofolio/app.py').stat().st_ino==source_inode
    assert (old/'server/app.py').read_text()=='blank'
    assert (old/'data').resolve()==data
