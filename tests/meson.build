pytest = find_program('pytest', required: false)

if pytest.found()
  test('pytest', pytest,
    args: [meson.current_source_dir()],
    workdir: meson.project_source_root(),
    env: {
      'PYTHONPATH': meson.project_source_root() / 'src',
      'JAMJAR_GRESOURCE': meson.project_build_root() / 'data' / 'ui' / 'jamjar.gresource',
    },
    suite: 'unit',
    timeout: 60,
  )
endif
