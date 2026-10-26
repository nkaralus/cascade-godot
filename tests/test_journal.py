import pytest

from cascade_godot import Journal, TxError


def test_append_read_state(tmp_path):
    j = Journal(tmp_path)
    assert j.state() == "EMPTY" and j.read() == []
    j.append({"t": "BEGIN", "x": 1})
    assert j.state() == "OPEN"
    j.append({"t": "COMMITTED"})
    assert j.state() == "COMMITTED" and [r["t"] for r in j.read()] == ["BEGIN", "COMMITTED"]


def test_torn_last_line_is_ignored(tmp_path):
    j = Journal(tmp_path)
    j.append({"t": "BEGIN"})
    with open(j.path, "ab") as f:
        f.write(b'{"t":"STAG')
    assert [r["t"] for r in j.read()] == ["BEGIN"] and j.state() == "OPEN"


@pytest.mark.parametrize("junk", [b"not json\n", b"[1]\n", b'{"no_t":1}\n', b"\xff\xfe\n"])
def test_corruption_in_the_middle_raises(tmp_path, junk):
    j = Journal(tmp_path)
    j.append({"t": "BEGIN"})
    with open(j.path, "ab") as f:
        f.write(junk)
    j.append({"t": "STAGED"})
    with pytest.raises(TxError) as e:
        j.read()
    assert e.value.code == "JOURNAL_CORRUPT"
