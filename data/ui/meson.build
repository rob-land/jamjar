blueprint_compiler = find_program('blueprint-compiler')

blueprint_sources = files(
  'window.blp',
  'server-row.blp',
  'login-dialog.blp',
  'library-page.blp',
  'album-page.blp',
  'artist-page.blp',
  'playlist-page.blp',
  'home-page.blp',
  'history-page.blp',
  'search-page.blp',
  'now-playing-bar.blp',
  'now-playing-page.blp',
  'queue-pane.blp',
  'prefs.blp',
  'help-overlay.blp',
)

blueprints = custom_target('blueprints',
  input:  blueprint_sources,
  output: '.',
  command: [blueprint_compiler, 'batch-compile', '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
)

gnome.compile_resources(
  'jamjar',
  'jamjar.gresource.xml',
  gresource_bundle: true,
  install: true,
  install_dir: pkgdatadir,
  dependencies: blueprints,
  source_dir: meson.current_build_dir(),
)
