# Usage

## Installation

To use ramses_cc, install it in Home Assistant either via HACS or using the `update.install` Action:

```yaml
   action: update.install
   target:
      entity_id: update.ramses_cc_update
   data:
      version: 0.52.1
```

## Documentation

We use [sphinx](https://www.sphinx-doc.org/en/master/usage/markdown.html) and
MyST [markup](https://myst-parser.readthedocs.io/en/latest/syntax/organising_content.html) to automatically create this code documentation from `docstr` annotations in our python code.

- Activate your virtual environment for ramses_cc as described in the [Wiki](https://github.com/ramses-rf/ramses_cc/wiki).

- Install the extra required dependencies by running ``pip install -r requirements_docs.txt`` so you can build a local set.

- Then, in a Terminal, enter `cd docs/` and run `sphinx-build -b html source build/html`.

- When the operation finishes, you can open the generated files from the `docs/build/html/` folder in a web browser.
