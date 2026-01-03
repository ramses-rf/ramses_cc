#!/usr/bin/env python3
# Configuration file for the ramses_rf Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import json
import os
import sys
from datetime import datetime

# sys.path.insert(0, os.path.abspath("."))  # required to import src
sys.path.insert(0, os.path.abspath("../../custom_components"))

#  VERSION
with open("../../custom_components/ramses_cc/manifest.json") as f:
    config = json.load(f)

version = config["version"]
authors = "D. Bonnes, E. Broerse"

# -- Project information -----------------------------------------------------
# see https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "Ramses RF"
copyright = f"{datetime.now().year}, {authors}"
author = authors
release = version

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration
# adapted following https://medium.com/@cissyshu/a-step-by-step-guide-to-automatic-documentation-using-sphinx-a697dbbce0e7

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",  # link to local code (button)
    "sphinx.ext.githubpages",
    "sphinx_design",
    "myst_parser",  # to use Markdown inside reST
    "sphinxcontrib.mermaid",  # to show mermaid diagrams in docs
]
pygments_style = "sphinx"  # enable syntax highlighting

templates_path = ["_templates"]
exclude_patterns: list[str] = []

# Language
language = "en"

# -- Options for HTML / Thema output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "furo"
html_static_path = ["_static"]
html_logo = "_static/ramses_rf_logo.png"
html_theme_options = {
    "announcement": "Build the new <em>ramses_cc</em> documentation locally",
}

# --- Extension config ----

# Autodoc
autodoc_default_options = {
    # Autodoc members
    "members": True,
    # Autodoc private members
    "private-members": True,
}

# Autosummary
autosummary_generate = True
autosummary_generate_overwrite = True

# Myst
myst_enable_extensions = ["colon_fence"]
myst_heading_anchors = 4
myst_fence_as_directive = ["mermaid"]
