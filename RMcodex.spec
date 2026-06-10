# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('templates', 'templates'), ('staticfiles', 'staticfiles'), ('static', 'static'), ('rehab_center', 'rehab_center'), ('operations', 'operations'), ('manage.py', '.')]
binaries = []
hiddenimports = ['ninja', 'django_htmx', 'whitenoise', 'reportlab', 'django_tasks', 'psycopg', 'psycopg.types', 'asgiref', 'sqlparse']
datas += collect_data_files('operations')
hiddenimports += collect_submodules('operations.migrations')
hiddenimports += collect_submodules('django.core.management')
hiddenimports += collect_submodules('django.contrib.admin.management')
hiddenimports += collect_submodules('django.contrib.auth.management')
tmp_ret = collect_all('django_tasks')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('django_htmx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('whitenoise')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('auditlog')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:\\РадостьМояАвтоматизация\\RMcodex\\scripts\\launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RMcodex',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RMcodex',
)
