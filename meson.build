project('jamjar',
  version: '0.1.0',
  meson_version: '>= 1.2.0',
  default_options: ['warning_level=2'],
)

i18n   = import('i18n')
gnome  = import('gnome')
python = import('python').find_installation('python3')

prefix      = get_option('prefix')
bindir      = prefix / get_option('bindir')
datadir     = prefix / get_option('datadir')
localedir   = prefix / get_option('localedir')
pkgdatadir  = datadir / meson.project_name()
moduledir   = python.get_install_dir() / 'jamjar'

app_id          = 'land.rob.Jamjar'
gettext_package = meson.project_name()

# Bare-string substitutions for XML/desktop files (no surrounding quotes).
conf = configuration_data()
conf.set('PYTHON',          python.full_path())
conf.set('VERSION',         meson.project_version())
conf.set('PKGDATADIR',      pkgdatadir)
conf.set('LOCALEDIR',       localedir)
conf.set('APP_ID',          app_id)
conf.set('GETTEXT_PACKAGE', gettext_package)

# Quoted-string substitutions for Python sources (paths/IDs need to be string literals).
py_conf = configuration_data()
py_conf.set('PYTHON',                python.full_path())
py_conf.set_quoted('VERSION',         meson.project_version())
py_conf.set_quoted('PKGDATADIR',      pkgdatadir)
py_conf.set_quoted('LOCALEDIR',       localedir)
py_conf.set_quoted('APP_ID',          app_id)
py_conf.set_quoted('GETTEXT_PACKAGE', gettext_package)

subdir('data')
subdir('jamjar')
subdir('po')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
