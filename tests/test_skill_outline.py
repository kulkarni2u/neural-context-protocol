from pathlib import Path

from ncp.skill_cache import outline_skill
from tests.test_skill_mcp import SKILL, _Env

DOC = """# Title

Intro line one.
More prose dropped.

## Code

Here is code:

```python
import os

def foo(x):
    return x

async def bar():
    pass

class Baz:
    def m(self):
        pass
```

## Shell

```sh
def not_python
make ship
```
Trailing prose.

```
class Untagged:
    x = 1
```
"""


def test_outline_rules():
    out = outline_skill(DOC).splitlines()
    assert "# Title" in out and "Intro line one." in out
    assert "More prose dropped." not in out
    assert "Here is code:" in out
    assert "def foo(x):" in out
    assert "async def bar():" in out
    assert "class Baz:" in out
    assert "    def m(self):" in out
    assert "    return x" not in out
    assert "  ... (11 lines; detail:full for code)" in out
    assert "```sh" in out
    assert "make ship" not in out and "def not_python" not in out
    assert "class Untagged:" in out
    assert "Trailing prose." in out  # first non-heading line under ## Shell


def test_outline_deterministic_and_empty():
    assert outline_skill(DOC) == outline_skill(DOC)
    assert outline_skill("") == ""


def test_handler_detail(tmp_path: Path):
    env = _Env(tmp_path)
    env.register(DOC)
    full = env.skill(action="get", skill_id=SKILL, pipeline_id="p1", session_id="s1")["result"]
    assert full.startswith("[NCP:SKILL]") and "detail:outline" not in full.splitlines()[0]
    assert full.split("\n", 1)[1].rstrip() == DOC.rstrip()
    out = env.skill(action="get", skill_id=SKILL, pipeline_id="p1", detail="outline", session_id="s1")["result"]
    header, body = out.split("\n", 1)
    assert header.endswith(" detail:outline")
    assert body == outline_skill(DOC)
    explicit = env.skill(action="get", skill_id=SKILL, pipeline_id="p1", detail="full", session_id="s2")["result"]
    assert explicit == full
