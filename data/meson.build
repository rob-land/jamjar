subdir('ui')
subdir('icons')

# Desktop file
desktop_file = i18n.merge_file(
  input:  configure_file(
    input:         'land.rob.Jamjar.desktop.in',
    output:        'land.rob.Jamjar.desktop.in',
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
    input:         'land.rob.Jamjar.metainfo.xml.in',
    output:        'land.rob.Jamjar.metainfo.xml.in',
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

# GSettings schema
install_data(
  'land.rob.Jamjar.gschema.xml',
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
