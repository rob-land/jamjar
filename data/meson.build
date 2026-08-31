subdir('ui')
subdir('icons')

# Desktop file
desktop_file = i18n.merge_file(
  input:  configure_file(
    input:         'land.rob.jamjar.desktop.in',
    output:        'land.rob.jamjar.desktop.in',
    configuration: conf,
  ),
  output:     application_id + '.desktop',
  type:       'desktop',
  po_dir:     '../po',
  install:    true,
  install_dir: datadir / 'applications',
)

desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  test('desktop-file-validate',
    desktop_validate, args: [desktop_file],
    suite: 'data',
  )
endif

# AppStream metainfo
appstream_file = i18n.merge_file(
  input:  configure_file(
    input:         'land.rob.jamjar.metainfo.xml.in',
    output:        'land.rob.jamjar.metainfo.xml.in',
    configuration: conf,
  ),
  output:     application_id + '.metainfo.xml',
  po_dir:     '../po',
  install:    true,
  install_dir: datadir / 'metainfo',
)

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('appstream-validate',
    appstreamcli,
    args: ['validate', '--no-net', '--explain', appstream_file],
    suite: 'data',
  )
endif

# GNOME Shell search provider. Shell reads this from the exported
# datadir and talks to the object the app registers on its own bus name.
install_data(
  'land.rob.jamjar.search-provider.ini',
  install_dir: datadir / 'gnome-shell' / 'search-providers',
)

# GSettings schema
install_data(
  'land.rob.jamjar.gschema.xml',
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test('schema-validate',
    compile_schemas,
    args: ['--strict', '--dry-run', meson.current_source_dir()],
    suite: 'data',
  )
endif
