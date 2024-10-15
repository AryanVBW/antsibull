<!--
Copyright (c) Ansible Project
GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
SPDX-License-Identifier: GPL-3.0-or-later
-->

<p align="center">
  <img src="https://your-logo-link.com" alt="antsibull logo" width="400">
</p>
<p align="center">
  
# 🐜 **antsibull** — *Ansible Build Scripts* 🚀

### A powerful tool for building various Ansible-related things! 🎯  
&nbsp;
[![💬 Discuss on Matrix](https://img.shields.io/matrix/antsibull:ansible.com.svg?server_fqdn=ansible-accounts.ems.host&label=Join%20the%20Conversation&logo=matrix)](https://matrix.to/#/#antsibull:ansible.com)
[![🚀 Nox](https://github.com/ansible-community/antsibull/actions/workflows/nox.yml/badge.svg)](https://github.com/ansible-community/antsibull/actions/workflows/nox.yml)
[![👷‍♂️ PyPI on GH](https://github.com/ansible-community/antsibull/workflows/👷%20dumb%20PyPI%20on%20GH%20pages/badge.svg?event=push&branch=main)](https://github.com/ansible-community/antsibull/actions?query=workflow%3A%22👷+dumb+PyPI+on+GH+pages%22+branch%3Amain)
[![Codecov badge](https://img.shields.io/codecov/c/github/ansible-community/antsibull)](https://codecov.io/gh/ansible-community/antsibull)
</p>
---

## 🚧 **Scripts Available** 

✨ **`antsibull-build`** — Builds Ansible 6+ from component collections.  
📜 [Documentation](https://github.com/ansible-community/antsibull/blob/main/docs/build-ansible.rst)  
🔗 Related projects:  
  - [antsibull-changelog](https://pypi.org/project/antsibull-changelog/)  
  - [antsibull-docs](https://pypi.org/project/antsibull-docs/)

📄 **Changelog**  
You can find all the changes in the [Antsibull changelog](https://github.com/ansible-community/antsibull/blob/main/CHANGELOG.md).  

🚨 Covered by the [Ansible Code of Conduct](https://docs.ansible.com/ansible/latest/community/code_of_conduct.html).  

---

## 🛡️ **Licensing**

This repository follows the [REUSE specification](https://reuse.software).  
💼 The default license: **GNU Public License v3+** ([Details here](LICENSES/GPL-3.0-or-later.txt)).  
💡 Code derived from CPython is licensed under Python 2.0 ([Details here](LICENSES/Python-2.0.1.txt)).

---

## 🔢 **Versioning & Compatibility**

Since version **0.1.0**, antsibull follows **semantic versioning** 🧮 and ensures no breaking changes to the command line API during a major release cycle.

❗ **Note:** antsibull is not meant to be used as a library.  

---

## 💻 **Development** 

### Quick Start

🚀 To run tests, install and run `nox`. That’s it! 🎉  
It will create virtual environments in `.nox` and handle everything for you! 💡

---

## 🛠️ **Antsibull Development Projects**

Antsibull depends on several projects:  
`antsibull-core`, `antsibull-changelog`, `antsibull-docs-parser`, `antsibull-docutils`, `antsibull-fileutils`.  

Use the `OTHER_ANTSIBULL_MODE` environment variable to customize how these dependencies are installed:

1. **auto** — Default behavior.  
2. **local** — Install from local paths.  
3. **git** — Install from the GitHub main branch.  
4. **pypi** — Install the latest version from PyPI.

---

## 🚨 **Running Specific Tests**

🧪 **Unit Tests** — `nox -e test`  
🧹 **Linters** — `nox -e lint`  
🛠 **Formatters** — `nox -e formatters`  
📊 **Code Quality** — `nox -e codeqa`  
📚 **Type Checking** — `nox -e typing`  
🔍 **Coverage** — `nox -e coverage`  

---

## ⚙️ **Complete Local Development Setup**  

Follow these steps to clone and install antsibull along with its dependencies:

```bash
git clone https://github.com/ansible-community/antsibull-changelog.git
git clone https://github.com/ansible-community/antsibull-core.git
git clone https://github.com/ansible-community/antsibull-docs-parser.git
git clone https://github.com/ansible-community/antsibull-docutils.git
git clone https://github.com/ansible-community/antsibull.git
cd antsibull
python3 -m venv venv
source ./venv/bin/activate
pip install -e '.[dev]' -e ../antsibull-changelog -e ../antsibull-core -e ../antsibull-docs-parser -e ../antsibull-docutils
nox
```
Here’s your guide in GitHub-friendly Markdown format with emojis for a visually appealing README section:

## 🚀 Creating a New Release

Follow these steps to create a new release smoothly:

### 1. 🔧 Bump the Version  
Run the following command to start the release process:

```bash
nox -e bump -- <version> <release_summary_message>
```

This will:

	•	📈 Update the package version in src/antsibull/__init__.py.
	•	📄 Generate a new changelog fragment in changelogs/fragments/<version>.yml with a summary section.
	•	📝 Run antsibull-changelog release and stage the files for git.
	•	📦 Commit the changes with the message Release <version>. and create a tag:

git tag -a -m 'antsibull <version>' <version>


	•	🛠️ Build an sdist and wheel using hatch build --clean, and clean up old artifacts in the dist/ folder.

2. 🔄 Push Changes

Push the changes and tags to your repository:

git push

3. 🏗️ Publish the Release

Once the CI tests pass on GitHub, publish the release to PyPI with:

```nox -e publish```
This will:
-	🚀 Publish the package to PyPI using hatch publish.
-	🔄 Bump the version to <version>.post0 for post-release.
-	📋 Commit the version bump with:

```git commit -m 'Post-release version bump.'```



4. 🔧 Push Final Changes

Finally, push the new tags and changes:

```git push --follow-tags```

