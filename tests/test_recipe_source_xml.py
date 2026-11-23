"""Each book recipe imported from BeerXML keeps a BeerXML file of its own (4.7.7.post65).

The Upload page keeps one file, config/upload/beer.xml, so after importing A and then B it holds B. A plugin that
reads a brew's planned values (OG, IBU, the mash pH) from it would read B's for a brew of A. So the Recipe Book keeps
each BeerXML import's recipe in a file of its own, recipes/<file id>.xml, and names it in the recipe's basic data:
source_xml, relative to the config folder, and source_xml_recipe, the NAME of the one RECIPE in it.

Odd text never loses a recipe: an upload that doesn't parse as it declares is read in UTF-8, Windows-1252 or
Latin-1, or else made plain English letters (cbpi.api.beerxml), and kept as UTF-8; names compare by their letters.

Each test fails on a core before post65.
"""

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from unittest import mock

import pytest
import yaml

import cbpi.controller.recipe_controller as recipe_controller
from cbpi.api import beerxml
from cbpi.controller.recipe_controller import RecipeController
from cbpi.controller.upload_controller import UploadController
from tests.test_recipe_pages import Folder, brew_steps, in_book, uploader

KEYS = ("source_xml", "source_xml_recipe")


def export(*names, encoding="UTF-8", doctype=b"", extra=b""):
    """A BeerXML export of the recipes `names`, in `encoding`."""
    recipes = b"".join(
        b"<RECIPE><NAME>" + n.encode(encoding) + b"</NAME><VERSION>1</VERSION><BOIL_TIME>60</BOIL_TIME>"
        b"<MASH><NAME>Single</NAME><PH>5.4</PH></MASH>" + extra + b"</RECIPE>\n" for n in names)
    return (b'<?xml version="1.0" encoding="' + encoding.encode() + b'"?>\n' + doctype +
            b"<RECIPES>\n" + recipes + b"</RECIPES>\n")


def kept_from(tmp_path, content, recipe_id, name):
    path = tmp_path / "beer.xml"
    path.write_bytes(content)
    return UploadController.__new__(UploadController)._kept_recipe(str(path), recipe_id, name)


def shape(element):
    """An element as (tag, attributes, text, children), so two parses of one recipe compare equal."""
    return (element.tag, dict(element.attrib), (element.text or "").strip(), [shape(c) for c in element])


def book(tmp_path):
    cbpi = mock.Mock()
    cbpi.config_folder = Folder(tmp_path)
    return RecipeController(cbpi)


def kept_yaml(tmp_path, file_id):
    return yaml.safe_load((tmp_path / "recipes" / "{}.yaml".format(file_id)).read_text())


def recipe(name):
    return dict(basic=dict(name=name, author="Bench"), steps=[dict(name="Rest", type="MashStep", props={})])


# ----------------------------------------------------------------------------------------------- the kept file

def test_the_kept_file_reads_the_upload_in_its_own_encoding_and_is_written_as_utf8(tmp_path):
    upload = export("Kölsch", encoding="ISO-8859-1", extra="<DISPLAY_GRAIN_TEMP>65.0 °F</DISPLAY_GRAIN_TEMP>"
                                                          .encode("ISO-8859-1"))
    assert b"K\xf6lsch" in upload and b"\xb0F" in upload
    kept, name = kept_from(tmp_path, upload, "1", "Kölsch")
    assert name == "Kölsch"
    assert kept.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<RECIPES>\n')
    found = ET.fromstring(kept).findall("RECIPE")
    assert [r.findtext("NAME") for r in found] == ["Kölsch"]
    assert found[0].findtext("DISPLAY_GRAIN_TEMP") == "65.0 °F"
    assert found[0].findtext("MASH/PH") == "5.4"


def test_the_kept_file_holds_only_the_recipe_imported_of_several(tmp_path):
    upload = export("Pale Ale", "Kölsch", "Stout")
    kept, name = kept_from(tmp_path, upload, "2", "Kölsch")
    found = ET.fromstring(kept).findall(".//RECIPE")
    assert name == "Kölsch" and len(found) == 1
    assert shape(found[0]) == shape(ET.fromstring(upload).findall("RECIPE")[1])


def test_the_kept_file_has_no_doctype_and_its_entities_written_out(tmp_path):
    upload = export("Kölsch", doctype=b'<!DOCTYPE RECIPES [<!ENTITY deg "&#176;">]>\n',
                     extra=b"<DISPLAY_GRAIN_TEMP>65.0 &deg;F</DISPLAY_GRAIN_TEMP><!-- a note -->")
    kept, _ = kept_from(tmp_path, upload, "1", "Kölsch")
    assert b"<!DOCTYPE" not in kept and b"<!ENTITY" not in kept and b"<!" not in kept
    assert ET.fromstring(kept).findtext("RECIPE/DISPLAY_GRAIN_TEMP") == "65.0 °F"


def test_a_recipe_too_big_to_keep_gets_no_file_and_the_log_says_why(tmp_path, caplog):
    big = b"<NOTES>" + b"x" * (2 * 1024 * 1024) + b"</NOTES>"
    with caplog.at_level(logging.WARNING):
        assert kept_from(tmp_path, export("Kölsch", extra=big), "1", "Kölsch") is None
    assert "Kölsch is kept in the Recipe Book without a BeerXML file of its own" in caplog.text
    assert "more than 2097152" in caplog.text
    kept, _ = kept_from(tmp_path, export("Kölsch", extra=big[:-200_000] + b"</NOTES>"), "1", "Kölsch")
    assert len(kept) <= 2 * 1024 * 1024


def test_no_file_when_the_upload_no_longer_has_the_recipe_by_that_name(tmp_path):
    assert kept_from(tmp_path, export("Stout"), "1", "Kölsch") is None
    assert kept_from(tmp_path, export("Kölsch"), "2", "Kölsch") is None


def test_an_import_hands_the_kept_file_to_the_book():
    upload, cbpi = uploader()
    read = upload.xml_recipe_creation

    async def with_file(recipe_id):
        await read(recipe_id)
        upload._staged["source_xml"] = (b"<RECIPES/>", "Craftbeerpi Test Recipe v6")

    upload.xml_recipe_creation = with_file
    ok, _ = asyncio.run(upload.import_recipe("xml", "1"))
    assert ok is True
    assert cbpi.recipe.keep_import.await_args.kwargs == {"source_xml": (b"<RECIPES/>", "Craftbeerpi Test Recipe v6")}


# ------------------------------------------------------------------------------- odd text never loses a recipe

def doc(declared, name, extra=b""):
    head = b'<?xml version="1.0" encoding="' + declared + b'"?>\n' if declared else b""
    return head + b"<RECIPES><RECIPE><NAME>" + name + b"</NAME>" + extra + b"</RECIPE></RECIPES>"


ODD = [
    # (what, bytes, the name read, the encoding it was read in, made plain)
    ("Latin-1 under a UTF-8 declaration", doc(b"UTF-8", b"K\xf6lsch"), "Kölsch", "cp1252", False),
    ("UTF-8 under an ISO-8859-1 declaration", doc(b"ISO-8859-1", "Kölsch".encode()), "KÃ¶lsch", "iso8859-1", False),
    ("no declaration, Latin-1", doc(None, b"K\xf6lsch"), "Kölsch", "cp1252", False),
    ("a UTF-8 byte-order mark", b"\xef\xbb\xbf" + doc(b"UTF-8", "Kölsch".encode()), "Kölsch", "utf-8", False),
    ("a byte-order mark before Latin-1", b"\xef\xbb\xbf" + doc(None, b"K\xf6lsch"), "Kölsch", "cp1252", False),
    ("Windows-1252 quote and dash", doc(None, b"Brewer\x92s \x96 K\xf6lsch"), "Brewer’s – Kölsch", "cp1252", False),
    ("UTF-8 under a US-ASCII declaration", doc(b"US-ASCII", "Kölsch".encode()), "Kölsch", "utf-8", False),
    ("a declared encoding Python doesn't know", doc(b"x-beersmith", b"K\xf6lsch"), "Kölsch", "cp1252", False),
    ("a byte Windows-1252 hasn't", doc(None, b"K\xf6lsch", b"<NOTES>\x81</NOTES>"), "Kölsch", "iso8859-1", False),
    ("NUL bytes and control characters", doc(b"ISO-8859-1", b"K\xf6lsch\x00\x01\x1b"), "Kolsch", "iso8859-1", True),
]


@pytest.mark.parametrize("what, content, name, encoding, made_plain", ODD, ids=[c[0] for c in ODD])
def test_odd_bytes_are_read_declared_then_utf8_then_windows_1252_then_latin1_then_plain(
        what, content, name, encoding, made_plain):
    found = beerxml.read(content)
    assert (found.root.findtext("RECIPE/NAME"), found.encoding, found.plain) == (name, encoding, made_plain)


@pytest.mark.parametrize("what, content, name, encoding, made_plain", ODD, ids=[c[0] for c in ODD])
def test_an_upload_is_kept_so_a_standard_parser_reads_it_and_the_log_says_how(
        what, content, name, encoding, made_plain, caplog):
    with caplog.at_level(logging.INFO):
        kept = UploadController._kept_bytes("xml", content)
    assert ET.fromstring(kept).findtext("RECIPE/NAME") == name
    try:
        ET.fromstring(content)
        as_sent = True
    except Exception:  # noqa: BLE001
        as_sent = False
    if as_sent:
        assert kept == content and "read in its declared encoding" in caplog.text
    else:
        assert kept.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        assert "read as {}".format(encoding) in caplog.text
        assert ("made plain English letters" in caplog.text) == made_plain


def test_plain_gives_plain_english_letters_and_drops_what_xml_forbids():
    assert beerxml.plain("Weißbier – Brewer’s ½ 65 °F ﬁ Øl Жж\x00\x01\x92\x81\t\n") == \
        "Weissbier - Brewer's 1/2 65 degF fi Ol '\t\n"


def test_names_compare_by_their_letters():
    assert beerxml.name_key("  Kölsch\tIPA ") == beerxml.name_key("KOLSCH ipa") == "kolsch ipa"
    assert beerxml.name_key("Weißbier") == beerxml.name_key("Weissbier")
    assert beerxml.name_key("Kölsch") != beerxml.name_key("Kolsch II")
    assert beerxml.name_key(None) == ""


def test_an_import_replaces_its_copy_by_name_exactly_first_then_by_its_letters(tmp_path):
    rc = book(tmp_path)
    kolsch, _ = asyncio.run(rc.keep_import(recipe("Kolsch"), "BeerXML"))
    again, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML"))
    assert (again, replaced) == (kolsch, True)
    other = "accented"
    in_book(tmp_path, other, dict(basic=dict(name="Kolsch", imported_from="BeerXML"), steps=[]))
    os.utime(tmp_path / "recipes" / "{}.yaml".format(kolsch), (1, 1))
    exact, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML"))
    assert (exact, replaced) == (kolsch, True)


def test_a_kept_file_that_cant_be_written_leaves_the_recipe_imported_without_one(tmp_path, caplog):
    rc = book(tmp_path)
    file_id, _ = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))

    def disk_full(path, content):
        raise OSError("disk full")

    with mock.patch.object(recipe_controller, "atomic_write_bytes", disk_full), caplog.at_level(logging.WARNING):
        again, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<B/>", "Kölsch")))
    assert (again, replaced) == (file_id, True)
    assert not any(k in kept_yaml(tmp_path, file_id)["basic"] for k in KEYS)
    assert sorted(os.listdir(tmp_path / "recipes")) == ["{}.yaml".format(file_id)]
    assert "Kölsch kept without a BeerXML file of its own" in caplog.text and "disk full" in caplog.text


# ---------------------------------------------------------------------------------------------- the Recipe Book

def test_the_book_keeps_the_file_beside_the_recipe_and_names_it(tmp_path):
    rc = book(tmp_path)
    file_id, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))
    basic = kept_yaml(tmp_path, file_id)["basic"]
    assert replaced is False
    assert basic["source_xml"] == "recipes/{}.xml".format(file_id)
    assert basic["source_xml_recipe"] == "Kölsch"
    assert (tmp_path / basic["source_xml"]).read_bytes() == b"<A/>"
    assert [r["name"] for r in asyncio.run(rc.get_recipes())] == ["Kölsch"]


def test_a_recipe_imported_again_gets_the_new_file_and_from_another_format_none(tmp_path):
    rc = book(tmp_path)
    file_id, _ = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))
    again, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<B/>", "Kölsch")))
    assert (again, replaced) == (file_id, True)
    assert (tmp_path / "recipes" / "{}.xml".format(file_id)).read_bytes() == b"<B/>"
    other, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "MMuM-JSON"))
    assert (other, replaced) == (file_id, True)
    assert not any(k in kept_yaml(tmp_path, file_id)["basic"] for k in KEYS)
    assert not (tmp_path / "recipes" / "{}.xml".format(file_id)).exists()


def test_a_book_recipe_whose_file_name_cant_name_one_gets_no_file(tmp_path):
    rc = book(tmp_path)
    in_book(tmp_path, "my kolsch", dict(basic=dict(name="Kölsch", imported_from="BeerXML"), steps=[]))
    file_id, replaced = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))
    assert (file_id, replaced) == ("my kolsch", True)
    assert not any(k in kept_yaml(tmp_path, file_id)["basic"] for k in KEYS)
    assert sorted(os.listdir(tmp_path / "recipes")) == ["my kolsch.yaml"]


@pytest.mark.parametrize("earlier", [False, True])
def test_a_recipe_that_cant_be_written_leaves_the_file_as_it_was(tmp_path, earlier):
    rc = book(tmp_path)
    if earlier:
        rc_id, _ = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))
    before = {p.name: p.read_bytes() for p in (tmp_path / "recipes").iterdir()}
    real = recipe_controller.atomic_write_text

    def disk_full(path, text, **kw):
        if str(path).endswith(".yaml"):
            raise OSError("disk full")
        return real(path, text, **kw)

    with mock.patch.object(recipe_controller, "atomic_write_text", disk_full):
        with pytest.raises(OSError):
            asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<B/>", "Kölsch")))
    assert {p.name: p.read_bytes() for p in (tmp_path / "recipes").iterdir()} == before


def test_deleting_a_book_recipe_deletes_its_file(tmp_path):
    rc = book(tmp_path)
    file_id, _ = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))
    other, _ = asyncio.run(rc.keep_import(recipe("Stout"), "BeerXML", source_xml=(b"<S/>", "Stout")))
    asyncio.run(rc.remove(file_id))
    assert sorted(os.listdir(tmp_path / "recipes")) == sorted(["{}.yaml".format(other), "{}.xml".format(other)])


def test_the_editors_save_keeps_the_books_file_and_a_clone_has_none(tmp_path):
    rc = book(tmp_path)
    file_id, _ = asyncio.run(rc.keep_import(recipe("Kölsch"), "BeerXML", source_xml=(b"<A/>", "Kölsch")))
    sent = recipe("Kölsch, less hops")
    sent["basic"].update(source_xml="../../step_data.json", source_xml_recipe="Stout")
    asyncio.run(rc.save(file_id, sent))
    basic = kept_yaml(tmp_path, file_id)["basic"]
    assert basic["name"] == "Kölsch, less hops"
    assert (basic["source_xml"], basic["source_xml_recipe"]) == ("recipes/{}.xml".format(file_id), "Kölsch")
    clone = asyncio.run(rc.clone(file_id, "Kölsch II"))
    assert not any(k in kept_yaml(tmp_path, clone)["basic"] for k in KEYS)
    assert not (tmp_path / "recipes" / "{}.xml".format(clone)).exists()


# ------------------------------------------------------------------------------------------ SAVE TO BOOK

SOURCE = {"source_xml": "recipes/kolsch.xml", "source_xml_recipe": "Kölsch"}


def test_save_to_book_gives_a_new_recipe_no_file_and_keeps_the_book_recipes_own_in_place(tmp_path):
    steps = brew_steps(tmp_path, dict(name="Kölsch", book_file="kolsch", **SOURCE))
    assert not any(k in steps.book_data()["basic"] for k in KEYS)
    as_new = asyncio.run(steps.savetobook(name="Kölsch II"))
    assert not any(k in kept_yaml(tmp_path, as_new)["basic"] for k in KEYS)
    no_body = asyncio.run(steps.savetobook())
    assert not any(k in kept_yaml(tmp_path, no_body)["basic"] for k in KEYS)
    in_book(tmp_path, "kolsch", dict(basic=dict(name="Kölsch", **SOURCE), steps=[]))
    in_book(tmp_path, "stout", dict(basic=dict(name="Stout", source_xml="recipes/stout.xml",
                                                source_xml_recipe="Stout"), steps=[]))
    in_book(tmp_path, "own", dict(basic=dict(name="My own"), steps=[]))
    for file_id, kept in (("kolsch", SOURCE), ("stout", {"source_xml": "recipes/stout.xml",
                                                         "source_xml_recipe": "Stout"}), ("own", {})):
        asyncio.run(steps.savetobook(file=file_id))
        basic = kept_yaml(tmp_path, file_id)["basic"]
        assert {k: basic[k] for k in KEYS if k in basic} == kept, file_id
    assert {k: steps.basic_data[k] for k in KEYS} == SOURCE
