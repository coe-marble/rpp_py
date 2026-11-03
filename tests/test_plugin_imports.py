"""Import isolation using temporary plugin files rather than generated RPP interfaces."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from rpp_py.plugin_loader import PythonPluginLoader, import_module_from_path


@pytest.fixture(autouse=True)
def restore_plugin_modules():
    previous = set(sys.modules)
    yield
    for name in set(sys.modules) - previous:
        if name.startswith("_rpp_plugin_"):
            del sys.modules[name]


def write_plugin(folder, source, filename="plugin.py"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(source, encoding="utf-8")
    return path


def test_identical_filenames_and_relative_helpers_are_isolated_between_libraries(tmp_path):
    paths = []
    for library, value in (("first", 1), ("second", 2)):
        folder = tmp_path / library / "plugins"
        paths.append(write_plugin(folder, "from .helper import VALUE\n"))
        write_plugin(folder, f"VALUE = {value}\n", "helper.py")
    original_path = list(sys.path)

    first, second = [import_module_from_path(path, allow_relative_imports=True)
                     for path in paths]

    assert first.VALUE == 1
    assert second.VALUE == 2
    assert first.__package__ != second.__package__
    assert sys.path == original_path
    assert import_module_from_path(paths[0], allow_relative_imports=True) is first


def test_repeated_import_uses_cached_module_without_reexecuting_source(tmp_path):
    path = write_plugin(tmp_path, "VALUES = []\n")
    first = import_module_from_path(path, allow_relative_imports=True)
    first.VALUES.append("retained")

    second = import_module_from_path(path, allow_relative_imports=True)

    assert second is first
    assert second.VALUES == ["retained"]


def test_equivalent_relative_and_absolute_paths_share_a_cached_module(tmp_path, monkeypatch):
    path = write_plugin(tmp_path, "VALUE = 1\n")
    monkeypatch.chdir(tmp_path)

    absolute = import_module_from_path(path, allow_relative_imports=True)
    relative = import_module_from_path(Path("plugin.py"), allow_relative_imports=True)

    assert relative is absolute


@pytest.mark.parametrize("relative_imports", [False, True])
def test_missing_plugin_path_reports_error_without_changing_import_state(
        tmp_path, relative_imports):
    previous = set(sys.modules)

    with pytest.raises(ValueError, match="does not exist"):
        import_module_from_path(tmp_path / "missing.py", allow_relative_imports=relative_imports)

    assert set(sys.modules) == previous


@pytest.mark.parametrize("source, exception", [
    ("raise ValueError('plugin import failed')\n", ValueError),
    ("class Plugin(:\n", SyntaxError),
    ("from .missing_helper import VALUE\n", ModuleNotFoundError),
])
def test_failed_import_removes_plugin_module_and_new_package(tmp_path, source, exception):
    path = write_plugin(tmp_path, source)
    previous = set(sys.modules)
    original_path = list(sys.path)

    with pytest.raises(exception):
        import_module_from_path(path, allow_relative_imports=True)

    assert not {name for name in set(sys.modules) - previous
                if name.startswith("_rpp_plugin_")}
    assert sys.path == original_path


def test_failed_sibling_import_preserves_existing_package_and_cached_plugin(tmp_path):
    good_path = write_plugin(tmp_path, "VALUE = 7\n", "good.py")
    good = import_module_from_path(good_path, allow_relative_imports=True)
    bad_path = write_plugin(tmp_path, "raise RuntimeError('bad sibling')\n", "bad.py")

    with pytest.raises(RuntimeError, match="bad sibling"):
        import_module_from_path(bad_path, allow_relative_imports=True)

    assert good.__package__ in sys.modules
    assert f"{good.__package__}.bad" not in sys.modules
    assert import_module_from_path(good_path, allow_relative_imports=True) is good
    assert good.VALUE == 7


def test_failed_import_does_not_leave_new_relative_helpers_cached(tmp_path):
    write_plugin(tmp_path, "VALUES = []\n", "helper.py")
    path = write_plugin(
        tmp_path, "from .helper import VALUES\nraise ValueError('import failed')\n")
    previous = set(sys.modules)

    with pytest.raises(ValueError, match="import failed"):
        import_module_from_path(path, allow_relative_imports=True)

    assert not {name for name in set(sys.modules) - previous
                if name.startswith("_rpp_plugin_")}


def test_retry_after_failed_import_uses_clean_helper_state(tmp_path):
    write_plugin(tmp_path, "VALUES = []\n", "helper.py")
    marker = tmp_path / "fail"
    marker.touch()
    path = write_plugin(tmp_path, (
        "from pathlib import Path\n"
        "from .helper import VALUES\n"
        "VALUES.append('loaded')\n"
        "if Path(__file__).with_name('fail').exists():\n"
        "    raise ValueError('import failed')\n"
        "RESULT = list(VALUES)\n"
    ))

    with pytest.raises(ValueError, match="import failed"):
        import_module_from_path(path, allow_relative_imports=True)
    marker.unlink()

    retried = import_module_from_path(path, allow_relative_imports=True)

    assert retried.RESULT == ["loaded"]


def test_failed_import_preserves_previously_loaded_helper_and_other_library(tmp_path):
    helper_path = write_plugin(tmp_path, "VALUE = object()\n", "helper.py")
    helper = import_module_from_path(helper_path, allow_relative_imports=True)
    other_path = write_plugin(tmp_path / "other_library", "VALUE = object()\n")
    other = import_module_from_path(other_path, allow_relative_imports=True)
    path = write_plugin(tmp_path, (
        "from .helper import VALUE\n"
        "from .new_helper import EXTRA\n"
        "raise ValueError('import failed')\n"
    ))
    write_plugin(tmp_path, "EXTRA = 1\n", "new_helper.py")

    with pytest.raises(ValueError, match="import failed"):
        import_module_from_path(path, allow_relative_imports=True)

    assert sys.modules[helper.__name__] is helper
    assert import_module_from_path(helper_path, allow_relative_imports=True) is helper
    assert import_module_from_path(other_path, allow_relative_imports=True) is other
    assert f"{helper.__package__}.new_helper" not in sys.modules


def test_failed_import_clears_helper_attribute_on_preexisting_package(tmp_path):
    good_path = write_plugin(tmp_path, "VALUE = 1\n", "good.py")
    good = import_module_from_path(good_path, allow_relative_imports=True)
    package = sys.modules[good.__package__]
    write_plugin(tmp_path, "VALUES = []\n", "helper.py")
    marker = tmp_path / "fail"
    marker.touch()
    path = write_plugin(tmp_path, (
        "from pathlib import Path\n"
        "from . import helper\n"
        "helper.VALUES.append('loaded')\n"
        "if Path(__file__).with_name('fail').exists():\n"
        "    raise ValueError('import failed')\n"
        "RESULT = list(helper.VALUES)\n"
    ))

    with pytest.raises(ValueError, match="import failed"):
        import_module_from_path(path, allow_relative_imports=True)

    assert sys.modules[good.__package__] is package
    assert not hasattr(package, "helper")
    marker.unlink()
    retried = import_module_from_path(path, allow_relative_imports=True)
    assert retried.RESULT == ["loaded"]
    assert sys.modules[good.__name__] is good


def test_plugin_loader_isolates_same_named_classes_and_returns_fresh_instances(tmp_path):
    paths = {library: write_plugin(tmp_path / library / "plugins", (
        f"class Controller:\n    library = {library!r}\n"
    )) for library in ("first", "second")}
    manager = Mock()
    manager.get_plugin_path_absolute.side_effect = lambda source, library: paths[library]
    available = {f"{library}::Controller": {"SourceFile": "plugins/plugin.py", "Library": library}
                 for library in paths}
    loader = PythonPluginLoader(manager, available_plugins=available)

    first = loader.create_instance("first::Controller")
    second = loader.create_instance("second::Controller")
    another_first = loader.create_instance("first::Controller")

    assert first.library == "first"
    assert second.library == "second"
    assert type(first) is type(another_first)
    assert type(first) is not type(second)
    assert first is not another_first
    assert manager.get_plugin_path_absolute.call_count == 2
    manager.get_plugin_info_from_lib.assert_not_called()


def test_plugin_loader_rejects_unavailable_plugin_before_accessing_library():
    manager = Mock()
    loader = PythonPluginLoader(manager, available_plugins={})

    with pytest.raises(ImportError, match="is not available"):
        loader.create_instance("library::Missing")

    manager.get_plugin_path_absolute.assert_not_called()
    manager.get_plugin_info_from_lib.assert_not_called()


def test_plugin_loader_reports_missing_class(tmp_path):
    path = write_plugin(tmp_path, "VALUE = 1\n")
    manager = Mock()
    manager.get_plugin_info_from_lib.return_value = {
        "SourceFile": "plugins/plugin.py", "Library": "library",
    }
    manager.get_plugin_path_absolute.return_value = path

    with pytest.raises(AttributeError, match="Plugin class 'Missing' not found"):
        PythonPluginLoader(manager).create_instance("library::Missing")
