"""Tests for .NET ecosystem extraction via tree-sitter.

Covers C# (.cs), Razor (.razor / .cshtml), VB.NET (.vb), and F# (.fs).
"""

from __future__ import annotations

from pathlib import Path

from code_memory.extractor.treesitter import (
    DEFAULT_IGNORE_DIRS,
    Extractor,
    extract_file,
    lang_for,
)

CSHARP_SRC = """\
namespace App.Foo;

using System;
using App.Bar;

public class Greeter
{
    public string Name { get; set; }

    public Greeter(string n)
    {
        Name = n;
    }

    public void Hello()
    {
        Console.WriteLine(Name);
        Helper.DoThing();
    }
}

public interface IFoo
{
    void Bar();
}

public struct Point
{
    public int X;
}

public record Person(string Name);

public enum Color
{
    Red,
    Green
}
"""

RAZOR_SRC = """\
@page "/foo"
@using App.Bar.Baz
@inject IService Svc

<h1>Hello</h1>

@code {
    public string Greeting { get; set; } = "hi";
    public void DoIt() { Svc.Run(); }
}
"""

CSHTML_SRC = """\
@using App.Bar
@{
    var x = 1;
}
<h1>@x</h1>
"""

VB_SRC = """\
Imports System
Imports App.Bar

Namespace App.Foo
    Public Class Greeter
        Public Sub Hello()
            Console.WriteLine("hi")
            Helper.DoThing()
        End Sub
    End Class
    Public Module Util
    End Module
End Namespace
"""

FSHARP_SRC = """\
module App.Foo
open System
open App.Bar

let greet name = name
let add x y = x + y

type Greeter() =
    member this.Hello() = 1
"""


# ---------------------------------------------------------------- ext mapping


def test_lang_for_dotnet_extensions() -> None:
    assert lang_for("Foo.cs") == "csharp"
    assert lang_for("Foo.razor") == "razor"
    assert lang_for("Foo.cshtml") == "razor"
    assert lang_for("Foo.vb") == "vb"
    assert lang_for("Foo.fs") == "fsharp"
    assert lang_for("Foo.fsi") == "fsharp"
    assert lang_for("Foo.fsx") == "fsharp"


# ---------------------------------------------------------------- C#


def test_extract_csharp(tmp_path: Path) -> None:
    f = tmp_path / "Greeter.cs"
    f.write_text(CSHARP_SRC)
    ex = extract_file(f)
    assert ex is not None and ex.lang == "csharp"
    names = {s.name for s in ex.symbols}
    assert {"Greeter", "Hello", "IFoo", "Point", "Person", "Color"} <= names
    assert "System" in ex.imports
    assert "App.Bar" in ex.imports
    assert "WriteLine" in ex.calls
    assert "DoThing" in ex.calls


# ---------------------------------------------------------------- Razor


def test_extract_razor(tmp_path: Path) -> None:
    f = tmp_path / "Foo.razor"
    f.write_text(RAZOR_SRC)
    ex = extract_file(f)
    assert ex is not None and ex.lang == "razor"
    names = {s.name for s in ex.symbols}
    assert {"Greeting", "DoIt"} <= names
    assert "App.Bar.Baz" in ex.imports
    assert "Run" in ex.calls


def test_extract_cshtml(tmp_path: Path) -> None:
    f = tmp_path / "Foo.cshtml"
    f.write_text(CSHTML_SRC)
    ex = extract_file(f)
    assert ex is not None and ex.lang == "razor"
    assert "App.Bar" in ex.imports


# ---------------------------------------------------------------- VB.NET


def test_extract_vb(tmp_path: Path) -> None:
    f = tmp_path / "Foo.vb"
    f.write_text(VB_SRC)
    ex = extract_file(f)
    assert ex is not None and ex.lang == "vb"
    names = {s.name for s in ex.symbols}
    assert "Greeter" in names
    assert "Hello" in names
    assert "Util" in names  # module_block
    assert "System" in ex.imports
    assert "App.Bar" in ex.imports
    assert "WriteLine" in ex.calls
    assert "DoThing" in ex.calls


# ---------------------------------------------------------------- F#


def test_extract_fsharp(tmp_path: Path) -> None:
    f = tmp_path / "Foo.fs"
    f.write_text(FSHARP_SRC)
    ex = extract_file(f)
    assert ex is not None and ex.lang == "fsharp"
    names = {s.name for s in ex.symbols}
    # let bindings and type defs surface as symbols
    assert "greet" in names
    assert "add" in names
    assert "Greeter" in names
    assert "App.Foo" in names  # named_module
    assert "System" in ex.imports
    assert "App.Bar" in ex.imports


# ---------------------------------------------------------------- walker dirs


def test_walker_skips_dotnet_build_dirs_by_default(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Main.cs").write_text(CSHARP_SRC)
    for skip in ("bin", "obj", "packages", "TestResults", ".vs", "artifacts"):
        (tmp_path / skip / "Debug").mkdir(parents=True)
        (tmp_path / skip / "Debug" / "Generated.cs").write_text(CSHARP_SRC)
        assert skip in DEFAULT_IGNORE_DIRS
    paths = {Path(ex.path) for ex in Extractor().walk(tmp_path)}
    src_file = (tmp_path / "src" / "Main.cs").resolve()
    assert src_file in paths
    for ex_path in paths:
        parts = set(ex_path.parts)
        for skip in ("bin", "obj", "packages", "TestResults", ".vs", "artifacts"):
            assert skip not in parts
