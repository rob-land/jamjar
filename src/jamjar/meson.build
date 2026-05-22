configure_file(
  input:         'const.py.in',
  output:        'const.py',
  configuration: py_conf,
  install:       true,
  install_dir:   moduledir,
)

configure_file(
  input:         'jamjar.in',
  output:        'jamjar',
  configuration: py_conf,
  install:       true,
  install_dir:   bindir,
  install_mode:  'rwxr-xr-x',
)

python_sources = files(
  '__init__.py',
  'main.py',
  'application.py',
  'window.py',
  'discovery.py',
  'auth.py',
  'secrets.py',
  'client.py',
  'models.py',
  'library.py',
  'imagecache.py',
  'sleep_timer.py',
  'player.py',
  'queue.py',
  'mpris.py',
  'scrobble.py',
  'lyrics.py',
  'offline.py',
)

python.install_sources(python_sources, subdir: 'jamjar')

views_sources = files(
  'views/__init__.py',
  'views/_common.py',
  'views/home.py',
  'views/history.py',
  'views/library.py',
  'views/album.py',
  'views/artist.py',
  'views/playlist.py',
  'views/search.py',
  'views/now_playing.py',
  'views/queue.py',
  'views/prefs.py',
  'views/login.py',
  'views/track_menu.py',
  'views/sleep_timer.py',
  'views/album_menu.py',
  'views/add_to_playlist.py',
)

python.install_sources(views_sources, subdir: 'jamjar/views')
