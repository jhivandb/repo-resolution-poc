# repo-resolution-poc

Recursively discover all WSO2 GitHub repositories that compose a product release.

Starting from a seed product repo (e.g. `wso2/product-apim` at a release tag), the
tool walks P2 profile metadata and Maven POM dependencies, maps WSO2 `groupId`
values to source repositories, and outputs a `repos.yaml` file listing every
discovered repository.

## Prerequisites

| Tool | Required | Purpose |
|------|----------|---------|
| [Python >= 3.10](https://www.python.org/) | Yes | Runtime |
| [uv](https://docs.astral.sh/uv/) | Yes | Runs the script with inline dependency metadata (installs `pyyaml` automatically) |
| [git](https://git-scm.com/) | Yes | Clones discovered repositories |
| [gh](https://cli.github.com/) | Yes | GitHub API lookups for repository resolution |
| [mvn](https://maven.apache.org/) | No | Only needed with `--use-maven` for full transitive dependency resolution |

## Usage

```bash
uv run resolve-wso2-repos.py <org/repo> [options]
```

### Positional argument

| Argument | Description |
|----------|-------------|
| `repo` | GitHub repository in `org/name` format (e.g. `wso2/product-apim`) |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--tag TAG` | *(default branch)* | Release tag to check out (e.g. `v4.3.0`) |
| `--groups PREFIXES` | `org.wso2,org.apache.synapse` | Comma-separated Maven groupId prefixes to consider in-scope |
| `--output PATH` | `./resolved-repos.yaml` | Path for the output YAML file |
| `--work-dir DIR` | `./workspace` | Directory where repos are cloned during discovery |
| `--max-depth N` | `3` | Maximum BFS traversal depth (`0` = unlimited) |
| `--use-maven` | *(off)* | Use `mvn dependency:tree` instead of fast POM XML parsing (slower, finds transitive deps) |
| `--maven-timeout SECS` | `600` | Timeout per Maven invocation (only with `--use-maven`) |
| `--clone-workers N` | `8` | Maximum number of parallel `git clone` workers |

## Examples

```bash
# Discover repos for APIM v4.3.0
uv run resolve-wso2-repos.py wso2/product-apim --tag v4.3.0

# Shallow depth, custom output location
uv run resolve-wso2-repos.py wso2/product-apim --tag v4.3.0 --max-depth 2 --output repos.yaml

# Full Maven resolution (slower, more thorough)
uv run resolve-wso2-repos.py wso2/product-apim --tag v4.3.0 --use-maven

# Identity Server
uv run resolve-wso2-repos.py wso2/product-is --tag v7.1.0

# Micro Integrator
uv run resolve-wso2-repos.py wso2/micro-integrator --tag v4.5.0
```

## Output

The tool writes a YAML file listing all discovered repositories:

```yaml
repos:
  - name: carbon-apimgt
    url: https://github.com/wso2/carbon-apimgt
    shallow: true
    tag: v9.30.67
  - name: carbon-kernel
    url: https://github.com/wso2/carbon-kernel
    shallow: true
    tag: v4.9.27
  # ... typically 50-200+ repos depending on the product
```

Each entry includes:

- **name** -- repository name
- **url** -- full GitHub clone URL
- **shallow** -- always `true` (shallow clones are used during discovery)
- **tag** -- *(optional)* git tag matching the dependency version pinned in the seed repo's POM (e.g. `v9.30.67`). Present for depth-1 repos whose version could be resolved from the seed's `<properties>`. Omitted when the version is unknown.

## How it works

1. **Seed** -- Clones the product repo at the specified tag
2. **Parse** -- Extracts dependencies from POM XML files and P2 profile metadata
3. **Resolve** -- Maps Maven `groupId` values to GitHub repositories using a built-in mapping table of 400+ WSO2 groupIds, with GitHub API fallback for unknown ones
4. **Recurse** -- BFS into each newly discovered repo up to `--max-depth`
5. **Output** -- Deduplicates and writes the final list to YAML

The default **fast mode** parses POM XML directly (seconds per repo). The optional `--use-maven` mode runs `mvn dependency:tree` for full transitive resolution (minutes per repo, but more complete).
