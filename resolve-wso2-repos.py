#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Recursively discover all WSO2 GitHub repos composing a product release.

Starts from a product repo (e.g. wso2/product-apim at a release tag),
discovers components via P2 profile parsing and direct POM XML parsing,
then recurses into each discovered repo to find more dependencies.

By default uses fast POM XML parsing (seconds per repo). Pass --use-maven
for full Maven dependency:tree resolution (minutes per repo, finds transitive deps).

Outputs a repos.yaml-compatible file for multi-repo scanning experiments.

Usage:
    uv run resolve-wso2-repos.py wso2/product-apim --tag v4.3.0
    uv run resolve-wso2-repos.py wso2/product-apim --tag v4.3.0 --use-maven
    uv run resolve-wso2-repos.py wso2/product-apim --tag v4.3.0 --max-depth 2
"""

import argparse
import concurrent.futures
import json
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

MAVEN_NS = "{http://maven.apache.org/POM/4.0.0}"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class MavenArtifact(NamedTuple):
    group_id: str
    artifact_id: str
    version: str = ""


@dataclass
class RepoEntry:
    name: str
    url: str
    org: str
    shallow: bool = True
    discovered_via: str = ""
    tag: str = ""  # git tag (e.g. "v9.30.67"), empty = default branch


# ---------------------------------------------------------------------------
# Static groupId -> repo mapping (from component-graph-poc/extract_graph.py)
# ---------------------------------------------------------------------------

GROUPID_TO_REPO: dict[str, dict[str, str]] = {
    "org.wso2.carbon.apimgt": {
        "repo": "carbon-apimgt",
        "url": "https://github.com/wso2/carbon-apimgt",
        "org": "wso2",
    },
    "org.wso2.carbon.apimgt.ui": {
        "repo": "apim-apps",
        "url": "https://github.com/wso2/apim-apps",
        "org": "wso2",
    },
    "org.wso2.am.analytics.publisher": {
        "repo": "apim-analytics-publisher",
        "url": "https://github.com/wso2/apim-analytics-publisher",
        "org": "wso2",
    },
    "org.wso2.carbon": {
        "repo": "carbon-kernel",
        "url": "https://github.com/wso2/carbon-kernel",
        "org": "wso2",
    },
    "org.wso2.carbon.commons": {
        "repo": "carbon-commons",
        "url": "https://github.com/wso2/carbon-commons",
        "org": "wso2",
    },
    "org.wso2.carbon.mediation": {
        "repo": "carbon-mediation",
        "url": "https://github.com/wso2/carbon-mediation",
        "org": "wso2",
    },
    "org.wso2.carbon.registry": {
        "repo": "carbon-registry",
        "url": "https://github.com/wso2/carbon-registry",
        "org": "wso2",
    },
    "org.wso2.carbon.governance": {
        "repo": "carbon-governance",
        "url": "https://github.com/wso2/carbon-governance",
        "org": "wso2",
    },
    "org.wso2.carbon.deployment": {
        "repo": "carbon-deployment",
        "url": "https://github.com/wso2/carbon-deployment",
        "org": "wso2",
    },
    "org.wso2.carbon.multitenancy": {
        "repo": "carbon-multitenancy",
        "url": "https://github.com/wso2/carbon-multitenancy",
        "org": "wso2",
    },
    "org.wso2.carbon.metrics": {
        "repo": "carbon-metrics",
        "url": "https://github.com/wso2/carbon-metrics",
        "org": "wso2",
    },
    "org.wso2.carbon.analytics-common": {
        "repo": "carbon-analytics-common",
        "url": "https://github.com/wso2/carbon-analytics-common",
        "org": "wso2",
    },
    "org.wso2.carbon.event-processing": {
        "repo": "carbon-event-processing",
        "url": "https://github.com/wso2/carbon-event-processing",
        "org": "wso2",
    },
    "org.wso2.carbon.messaging": {
        "repo": "carbon-business-messaging",
        "url": "https://github.com/wso2/carbon-business-messaging",
        "org": "wso2",
    },
    "org.wso2.carbon.consent.mgt": {
        "repo": "carbon-consent-management",
        "url": "https://github.com/wso2/carbon-consent-management",
        "org": "wso2",
    },
    "org.wso2.carbon.utils": {
        "repo": "carbon-utils",
        "url": "https://github.com/wso2/carbon-utils",
        "org": "wso2",
    },
    "org.wso2.carbon.identity.framework": {
        "repo": "carbon-identity-framework",
        "url": "https://github.com/wso2/carbon-identity-framework",
        "org": "wso2",
    },
    "org.wso2.carbon.identity.governance": {
        "repo": "identity-governance",
        "url": "https://github.com/wso2-extensions/identity-governance",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.inbound.auth.oauth2": {
        "repo": "identity-inbound-auth-oauth",
        "url": "https://github.com/wso2-extensions/identity-inbound-auth-oauth",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.inbound.auth.openid": {
        "repo": "identity-inbound-auth-openid",
        "url": "https://github.com/wso2-extensions/identity-inbound-auth-openid",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.outbound.auth.saml2": {
        "repo": "identity-outbound-auth-saml2",
        "url": "https://github.com/wso2-extensions/identity-outbound-auth-saml2",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.outbound.auth.oidc": {
        "repo": "identity-outbound-auth-oidc",
        "url": "https://github.com/wso2-extensions/identity-outbound-auth-oidc",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.application.auth.basic": {
        "repo": "identity-local-auth-basicauth",
        "url": "https://github.com/wso2-extensions/identity-local-auth-basicauth",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.auth.rest": {
        "repo": "identity-carbon-auth-rest",
        "url": "https://github.com/wso2-extensions/identity-carbon-auth-rest",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.event.handler.notification": {
        "repo": "identity-event-handler-notification",
        "url": "https://github.com/wso2-extensions/identity-event-handler-notification",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.event.handler.accountlock": {
        "repo": "identity-event-handler-account-lock",
        "url": "https://github.com/wso2-extensions/identity-event-handler-account-lock",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.data.publisher.oauth": {
        "repo": "identity-data-publisher-oauth",
        "url": "https://github.com/wso2-extensions/identity-data-publisher-oauth",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.datapublisher.authentication": {
        "repo": "identity-data-publisher-authentication",
        "url": "https://github.com/wso2-extensions/identity-data-publisher-authentication",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.metadata.saml2": {
        "repo": "identity-metadata-saml2",
        "url": "https://github.com/wso2-extensions/identity-metadata-saml2",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.user.ws": {
        "repo": "identity-user-ws",
        "url": "https://github.com/wso2-extensions/identity-user-ws",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.carbon.auth.saml2": {
        "repo": "identity-carbon-auth-saml2",
        "url": "https://github.com/wso2-extensions/identity-carbon-auth-saml2",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.saml.common": {
        "repo": "carbon-identity-saml-common",
        "url": "https://github.com/wso2-extensions/carbon-identity-saml-common",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.organization.management": {
        "repo": "identity-organization-management",
        "url": "https://github.com/wso2-extensions/identity-organization-management",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.identity.organization.management.core": {
        "repo": "identity-organization-management-core",
        "url": "https://github.com/wso2/identity-organization-management-core",
        "org": "wso2",
    },
    "org.wso2.carbon.identity.branding.preference.management": {
        "repo": "identity-branding-preference-management",
        "url": "https://github.com/wso2-extensions/identity-branding-preference-management",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.extension.identity.oauth2.grantType.token.exchange": {
        "repo": "identity-oauth2-grant-token-exchange",
        "url": "https://github.com/wso2-extensions/identity-oauth2-grant-token-exchange",
        "org": "wso2-extensions",
    },
    "org.wso2.identity.apps": {
        "repo": "identity-apps",
        "url": "https://github.com/wso2/identity-apps",
        "org": "wso2",
    },
    "org.apache.synapse": {
        "repo": "wso2-synapse",
        "url": "https://github.com/wso2/wso2-synapse",
        "org": "wso2",
    },
    "org.wso2.transport.http": {
        "repo": "transport-http",
        "url": "https://github.com/wso2/transport-http",
        "org": "wso2",
    },
    "org.wso2.km.ext.wso2is": {
        "repo": "apim-km-wso2is",
        "url": "https://github.com/wso2-extensions/apim-km-wso2is",
        "org": "wso2-extensions",
    },
    "org.wso2.km.ext.okta": {
        "repo": "apim-km-okta",
        "url": "https://github.com/wso2-extensions/apim-km-okta",
        "org": "wso2-extensions",
    },
    "org.wso2.km.ext.azure": {
        "repo": "apim-km-azure-ad",
        "url": "https://github.com/wso2-extensions/apim-km-azure-ad",
        "org": "wso2-extensions",
    },
    "org.wso2.km.ext.keycloak": {
        "repo": "apim-km-keycloak",
        "url": "https://github.com/wso2-extensions/apim-km-keycloak",
        "org": "wso2-extensions",
    },
    "org.wso2.km.ext.auth0": {
        "repo": "apim-km-auth0",
        "url": "https://github.com/wso2-extensions/apim-km-auth0",
        "org": "wso2-extensions",
    },
    "org.wso2.km.ext.pingfederate": {
        "repo": "apim-km-pingfederate",
        "url": "https://github.com/wso2-extensions/apim-km-pingfederate",
        "org": "wso2-extensions",
    },
    "org.wso2.km.ext.forgerock": {
        "repo": "apim-km-forgerock",
        "url": "https://github.com/wso2-extensions/apim-km-forgerock",
        "org": "wso2-extensions",
    },
    "org.wso2.gw.ext": {
        "repo": "apim-gw-connectors",
        "url": "https://github.com/wso2-extensions/apim-gw-connectors",
        "org": "wso2-extensions",
    },
    "org.wso2.am": {
        "repo": "product-apim",
        "url": "https://github.com/wso2/product-apim",
        "org": "wso2",
    },
    "org.wso2.ciphertool": {
        "repo": "carbon-kernel",
        "url": "https://github.com/wso2/carbon-kernel",
        "org": "wso2",
    },
    # --- Additional repos discovered via Maven (added for static coverage) ---
    "org.wso2.andes": {
        "repo": "andes",
        "url": "https://github.com/wso2/andes",
        "org": "wso2",
    },
    "org.wso2.balana": {
        "repo": "balana",
        "url": "https://github.com/wso2/balana",
        "org": "wso2",
    },
    "org.wso2.charon": {
        "repo": "charon",
        "url": "https://github.com/wso2/charon",
        "org": "wso2",
    },
    "org.wso2.siddhi": {
        "repo": "siddhi",
        "url": "https://github.com/wso2/siddhi",
        "org": "wso2",
    },
    "org.wso2.msf4j": {
        "repo": "msf4j",
        "url": "https://github.com/wso2/msf4j",
        "org": "wso2",
    },
    "org.wso2.jaggery": {
        "repo": "jaggery",
        "url": "https://github.com/wso2/jaggery",
        "org": "wso2",
    },
    "org.wso2.carbon.analytics": {
        "repo": "carbon-analytics",
        "url": "https://github.com/wso2/carbon-analytics",
        "org": "wso2",
    },
    "org.wso2.carbon.caching": {
        "repo": "carbon-caching",
        "url": "https://github.com/wso2/carbon-caching",
        "org": "wso2",
    },
    "org.wso2.carbon.securevault": {
        "repo": "carbon-securevault-hashicorp",
        "url": "https://github.com/wso2-extensions/carbon-securevault-hashicorp",
        "org": "wso2-extensions",
    },
    "org.wso2.carbon.transports": {
        "repo": "carbon-transports",
        "url": "https://github.com/wso2/carbon-transports",
        "org": "wso2",
    },
    "org.wso2.carbon.maven": {
        "repo": "carbon-maven-plugins",
        "url": "https://github.com/wso2/carbon-maven-plugins",
        "org": "wso2",
    },
    "org.wso2.config.mapper": {
        "repo": "config-mapper",
        "url": "https://github.com/wso2/config-mapper",
        "org": "wso2",
    },
    "org.wso2.tomcat.extensions": {
        "repo": "tomcat-extension-samlsso",
        "url": "https://github.com/wso2-extensions/tomcat-extension-samlsso",
        "org": "wso2-extensions",
    },
    "org.wso2.is": {
        "repo": "product-is",
        "url": "https://github.com/wso2/product-is",
        "org": "wso2",
    },
    "org.wso2.carbon.identity.api": {
        "repo": "identity-api-stub-generators",
        "url": "https://github.com/wso2/identity-api-stub-generators",
        "org": "wso2",
    },
    # WSO2 fork repos (groupIds use original project groupIds with .wso2 suffix)
    "commons-httpclient.wso2": {
        "repo": "wso2-commons-httpclient",
        "url": "https://github.com/wso2/wso2-commons-httpclient",
        "org": "wso2",
    },
    "org.wso2.orbit.org.apache.mina": {
        "repo": "wso2-mina",
        "url": "https://github.com/wso2/wso2-mina",
        "org": "wso2",
    },
    "org.wso2.orbit.com.nimbusds": {
        "repo": "wso2-nimbus-jose-jwt",
        "url": "https://github.com/wso2/wso2-nimbus-jose-jwt",
        "org": "wso2",
    },
    "org.wso2.orbit.org.apache.openjpa": {
        "repo": "wso2-openjpa",
        "url": "https://github.com/wso2/wso2-openjpa",
        "org": "wso2",
    },
    "org.wso2.pax.logging": {
        "repo": "wso2-pax-logging",
        "url": "https://github.com/wso2/wso2-pax-logging",
        "org": "wso2",
    },
    "wsdl4j.wso2": {
        "repo": "wso2-wsdl4j",
        "url": "https://github.com/wso2/wso2-wsdl4j",
        "org": "wso2",
    },
    "org.wso2.api.specs": {
        "repo": "api-specs",
        "url": "https://github.com/wso2/api-specs",
        "org": "wso2",
    },
}


# Repos that show up via GitHub search but aren't actual APIM dependencies
EXCLUDED_REPOS: set[str] = {
    "aws-cicd-deployment-scripts",
    "azure-terraform-modules",
    "puppet-apim",
    "puppet-esb",
    "choreo-samples",
    "choreo-sample-book-list-service",
    "integration-studio-examples",
    "esb-connector-mailchimp",
    "mi-connector-core",
    "mi-connector-rabbitmq",
    "mi-inbound-pulsar",
    "ballerina-mi-module-gen-tool",
    "product-integrator-websubhub",
}


# ---------------------------------------------------------------------------
# GroupId -> Repo resolution
# ---------------------------------------------------------------------------

# Cache for GitHub API lookups (positive and negative)
_github_cache: dict[str, RepoEntry | None] = {}

# Cache at groupId level so the same groupId with different artifactIds
# doesn't repeat the full candidate generation + API search loop
_groupid_cache: dict[str, RepoEntry | None] = {}


def resolve_groupid_to_repo(group_id: str) -> RepoEntry | None:
    """Map a Maven groupId to its source repository using longest-prefix match."""
    best_match = None
    best_len = 0
    for prefix, info in GROUPID_TO_REPO.items():
        if group_id.startswith(prefix) and len(prefix) > best_len:
            best_match = info
            best_len = len(prefix)
    if best_match:
        return RepoEntry(
            name=best_match["repo"],
            url=best_match["url"],
            org=best_match["org"],
        )
    return None


def _gh_repo_exists(org: str, name: str) -> RepoEntry | None:
    """Check if a GitHub repo exists using gh CLI. Returns RepoEntry or None."""
    cache_key = f"{org}/{name}"
    if cache_key in _github_cache:
        return _github_cache[cache_key]

    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{org}/{name}", "--jq", ".full_name"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            entry = RepoEntry(
                name=name,
                url=f"https://github.com/{org}/{name}",
                org=org,
            )
            _github_cache[cache_key] = entry
            return entry
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    _github_cache[cache_key] = None
    return None


def _gh_search_repo(query: str, org: str) -> RepoEntry | None:
    """Search for a repo in a GitHub org. Returns first match or None."""
    cache_key = f"search:{org}:{query}"
    if cache_key in _github_cache:
        return _github_cache[cache_key]

    try:
        result = subprocess.run(
            ["gh", "api", f"/search/repositories?q=org:{org}+{query}",
             "--jq", ".items[0].full_name"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            full_name = result.stdout.strip()
            repo_org, repo_name = full_name.split("/", 1)
            entry = RepoEntry(
                name=repo_name,
                url=f"https://github.com/{full_name}",
                org=repo_org,
            )
            _github_cache[cache_key] = entry
            return entry
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        pass

    _github_cache[cache_key] = None
    return None


def resolve_via_github(group_id: str, artifact_id: str) -> RepoEntry | None:
    """Fallback: resolve a Maven artifact to a GitHub repo via gh CLI."""
    # Check groupId-level cache first — same groupId always maps to same repo
    if group_id in _groupid_cache:
        cached = _groupid_cache[group_id]
        if cached:
            log.info(f"  GitHub cached: {group_id} -> {cached.org}/{cached.name}")
        return cached

    # Heuristic: build candidate repo names from the groupId
    candidates = set()

    # Strategy 1: strip org.wso2. prefix, replace . with -
    # e.g. org.wso2.carbon.apimgt -> carbon-apimgt
    for prefix in ("org.wso2.", "org.wso2.carbon.", "org.apache."):
        if group_id.startswith(prefix):
            remainder = group_id[len(prefix):]
            candidate = remainder.replace(".", "-")
            candidates.add(candidate)
            # Also try with "carbon-" prefix if stripped
            if prefix == "org.wso2." and not candidate.startswith("carbon-"):
                candidates.add(f"carbon-{candidate}")

    # Strategy 2: use the top-level artifactId directly
    # (often the parent POM artifactId matches the repo name)
    candidates.add(artifact_id)

    for candidate in candidates:
        # Try wso2 org first (more common), then wso2-extensions
        for org in ("wso2", "wso2-extensions"):
            entry = _gh_repo_exists(org, candidate)
            if entry:
                log.info(f"  GitHub lookup: {group_id} -> {org}/{candidate}")
                _groupid_cache[group_id] = entry
                return entry

    # Last resort: search API
    for org in ("wso2", "wso2-extensions"):
        entry = _gh_search_repo(group_id.split(".")[-1], org)
        if entry:
            log.info(f"  GitHub search: {group_id} -> {entry.org}/{entry.name}")
            _groupid_cache[group_id] = entry
            return entry

    _groupid_cache[group_id] = None
    return None


def resolve_artifact(artifact: MavenArtifact) -> RepoEntry | None:
    """Resolve a Maven artifact to a GitHub repo. Static table first, then GitHub API."""
    entry = resolve_groupid_to_repo(artifact.group_id)
    if entry and entry.name not in EXCLUDED_REPOS:
        return entry
    if entry:
        return None
    result = resolve_via_github(artifact.group_id, artifact.artifact_id)
    if result and result.name in EXCLUDED_REPOS:
        return None
    return result


# ---------------------------------------------------------------------------
# P2 profile parsing
# ---------------------------------------------------------------------------

def extract_p2_features(product_path: Path) -> list[MavenArtifact]:
    """Parse the P2 profile POM to find bundled feature artifacts."""
    p2_pom_candidates = [
        "all-in-one-apim/modules/p2-profile/product/pom.xml",
        "modules/p2-profile/product/pom.xml",
        "modules/p2-profile-gen/pom.xml",
    ]

    p2_pom = None
    for candidate in p2_pom_candidates:
        path = product_path / candidate
        if path.exists():
            p2_pom = path
            break

    if not p2_pom:
        log.warning(f"No P2 profile POM found in {product_path}")
        return []

    log.info(f"Parsing P2 profile: {p2_pom}")
    tree = ET.parse(p2_pom)
    root = tree.getroot()

    features = root.findall(f".//{MAVEN_NS}featureArtifactDef")
    log.info(f"Found {len(features)} feature artifact definitions")

    artifacts = []
    for feat in features:
        text = feat.text.strip()
        parts = text.split(":")
        if len(parts) < 2:
            continue
        group_id = parts[0]
        artifact_id = parts[1]
        version = parts[2] if len(parts) > 2 else ""
        artifacts.append(MavenArtifact(group_id, artifact_id, version))

    return artifacts


# ---------------------------------------------------------------------------
# POM property & version helpers
# ---------------------------------------------------------------------------

_VERSION_RANGE_RE = re.compile(r"^[\[\(]")
_PROPERTY_REF_RE = re.compile(r"\$\{(.+?)}")


def parse_pom_properties(repo_path: Path) -> dict[str, str]:
    """Parse <properties> from the root pom.xml into a name->value map."""
    root_pom = repo_path / "pom.xml"
    if not root_pom.exists():
        return {}

    try:
        tree = ET.parse(root_pom)
    except ET.ParseError:
        return {}

    root = tree.getroot()
    props_el = root.find(f"{MAVEN_NS}properties")
    if props_el is None:
        return {}

    props: dict[str, str] = {}
    for child in props_el:
        # Strip Maven namespace from tag name
        tag_name = child.tag.replace(MAVEN_NS, "")
        value = (child.text or "").strip()
        if value:
            props[tag_name] = value

    # One pass of self-resolution for nested ${prop} references
    for key, value in props.items():
        m = _PROPERTY_REF_RE.search(value)
        if m and m.group(1) in props:
            props[key] = _PROPERTY_REF_RE.sub(
                lambda m2: props.get(m2.group(1), m2.group(0)), value,
            )

    return props


def resolve_version(raw: str, properties: dict[str, str]) -> str:
    """Resolve a version string, substituting ${property} references."""
    if not raw:
        return ""
    m = _PROPERTY_REF_RE.fullmatch(raw)
    if m:
        return properties.get(m.group(1), "")
    return raw


def version_to_tag(version: str) -> str:
    """Convert a Maven version to a git tag using WSO2 v-prefix convention."""
    if not version or _VERSION_RANGE_RE.match(version):
        return ""
    return f"v{version}"


# ---------------------------------------------------------------------------
# Direct POM XML parsing (fast alternative to Maven)
# ---------------------------------------------------------------------------

_SKIP_DIRS = {"test", "target", "node_modules", ".git", "src/test"}


def parse_pom_dependencies_in_repo(
    repo_path: Path,
    group_prefixes: list[str],
    properties: dict[str, str] | None = None,
) -> set[MavenArtifact]:
    """Extract WSO2 dependencies by parsing all pom.xml files directly.

    Much faster than mvn dependency:tree (milliseconds vs minutes).
    Captures direct dependencies but not transitive ones.
    When *properties* is provided, resolves ${…} version references.
    """
    artifacts = set()

    for pom_path in repo_path.rglob("pom.xml"):
        # Skip test/target/node_modules directories
        parts = set(pom_path.relative_to(repo_path).parts)
        if parts & _SKIP_DIRS:
            continue

        try:
            tree = ET.parse(pom_path)
        except ET.ParseError:
            continue

        root = tree.getroot()

        # Extract <parent> groupId/artifactId/version
        parent = root.find(f"{MAVEN_NS}parent")
        if parent is not None:
            gid_el = parent.find(f"{MAVEN_NS}groupId")
            aid_el = parent.find(f"{MAVEN_NS}artifactId")
            if gid_el is not None and aid_el is not None:
                gid = (gid_el.text or "").strip()
                aid = (aid_el.text or "").strip()
                if any(gid.startswith(p) for p in group_prefixes):
                    ver_el = parent.find(f"{MAVEN_NS}version")
                    ver = (ver_el.text or "").strip() if ver_el is not None else ""
                    if properties:
                        ver = resolve_version(ver, properties)
                    artifacts.add(MavenArtifact(gid, aid, ver))

        # Extract all <dependency> groupId/artifactId/version
        for dep in root.findall(f".//{MAVEN_NS}dependency"):
            gid_el = dep.find(f"{MAVEN_NS}groupId")
            aid_el = dep.find(f"{MAVEN_NS}artifactId")
            if gid_el is None or aid_el is None:
                continue
            gid = (gid_el.text or "").strip()
            aid = (aid_el.text or "").strip()
            if any(gid.startswith(p) for p in group_prefixes):
                ver_el = dep.find(f"{MAVEN_NS}version")
                ver = (ver_el.text or "").strip() if ver_el is not None else ""
                if properties:
                    ver = resolve_version(ver, properties)
                artifacts.add(MavenArtifact(gid, aid, ver))

    return artifacts


# ---------------------------------------------------------------------------
# Maven dependency tree parsing (legacy, opt-in via --use-maven)
# ---------------------------------------------------------------------------

# Regex to strip tree-drawing characters from mvn dependency:tree output
_TREE_LINE_RE = re.compile(
    r"^\[INFO\]\s+"        # [INFO] prefix
    r"(?:[+\\|][\s\-]*)*"  # tree-drawing: +- \- |
    r"(.+)$"               # the actual dependency coordinate
)


def parse_dependency_tree(output: str, group_prefixes: list[str]) -> set[MavenArtifact]:
    """Parse mvn dependency:tree text output into a set of MavenArtifacts."""
    artifacts = set()

    for line in output.splitlines():
        m = _TREE_LINE_RE.match(line)
        if not m:
            continue

        coord = m.group(1).strip()
        parts = coord.split(":")
        # Expected: groupId:artifactId:packaging:version[:scope]
        # or:       groupId:artifactId:packaging:classifier:version[:scope]
        if len(parts) < 4:
            continue

        group_id = parts[0]
        artifact_id = parts[1]
        # version is at index 3 (no classifier) or 4 (with classifier)
        version = parts[4] if len(parts) >= 5 and parts[2] != "pom" else parts[3]

        if any(group_id.startswith(prefix) for prefix in group_prefixes):
            artifacts.add(MavenArtifact(group_id, artifact_id, version))

    return artifacts


def run_dependency_tree(
    repo_path: Path, group_prefixes: list[str], timeout: int = 600,
) -> set[MavenArtifact]:
    """Run mvn dependency:tree on a repo and parse the output."""
    pom = repo_path / "pom.xml"
    if not pom.exists():
        log.warning(f"No pom.xml in {repo_path}, skipping dependency:tree")
        return set()

    include_groups = ",".join(group_prefixes)
    cmd = [
        "mvn", "dependency:tree",
        f"-DincludeGroupIds={include_groups}",
        "-DskipTests",
        "--batch-mode",
        "-f", str(pom),
    ]

    log.info(f"Running mvn dependency:tree on {repo_path.name} (timeout {timeout}s)...")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.warning(f"mvn dependency:tree failed for {repo_path.name} (exit {result.returncode})")
            # Still try to parse partial output
        return parse_dependency_tree(result.stdout, group_prefixes)
    except subprocess.TimeoutExpired:
        log.warning(f"mvn dependency:tree timed out for {repo_path.name} after {timeout}s")
        return set()


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def clone_at_tag(url: str, dest: Path, tag: str | None = None) -> bool:
    """Clone a repo, optionally at a specific tag. Falls back to default branch."""
    if dest.exists() and (dest / ".git").exists():
        log.info(f"  Already cloned: {dest.name}")
        return True

    cmd = ["git", "clone", "--depth", "1"]
    if tag:
        cmd += ["--branch", tag]
    cmd += [url, str(dest)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
        log.info(f"  Cloned {dest.name}" + (f" at {tag}" if tag else ""))
        return True
    except subprocess.CalledProcessError:
        if tag:
            # Retry without tag (fall back to default branch)
            log.warning(f"  Tag {tag} not found for {dest.name}, using default branch")
            cmd_fallback = ["git", "clone", "--depth", "1", url, str(dest)]
            try:
                subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=120, check=True)
                log.info(f"  Cloned {dest.name} (default branch)")
                return True
            except subprocess.CalledProcessError as e:
                log.error(f"  Failed to clone {dest.name}: {e}")
                return False
        log.error(f"  Failed to clone {dest.name}")
        return False
    except subprocess.TimeoutExpired:
        log.error(f"  Clone timed out for {dest.name}")
        return False


# ---------------------------------------------------------------------------
# Parallel cloning
# ---------------------------------------------------------------------------

def clone_batch(
    entries: list[tuple[str, str, str | None]],
    work_dir: Path,
    max_workers: int = 8,
) -> dict[str, bool]:
    """Clone multiple repos in parallel.

    entries: list of (repo_name, url, tag_or_None)
    Returns dict of repo_name -> success.
    """
    results: dict[str, bool] = {}

    def _clone(item: tuple[str, str, str | None]) -> tuple[str, bool]:
        name, url, tag = item
        dest = work_dir / name
        return name, clone_at_tag(url, dest, tag)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for name, ok in pool.map(_clone, entries):
            results[name] = ok

    return results


# ---------------------------------------------------------------------------
# BFS discovery
# ---------------------------------------------------------------------------

def discover_repos(
    seed_repo: str,
    tag: str | None,
    group_prefixes: list[str],
    work_dir: Path,
    max_depth: int,
    use_maven: bool = False,
    maven_timeout: int = 600,
    clone_workers: int = 8,
) -> dict[str, RepoEntry]:
    """Recursively discover all WSO2 repos composing a product.

    By default uses fast POM XML parsing. Use use_maven=True for full
    Maven dependency:tree resolution (much slower).

    Returns a dict of repo_name -> RepoEntry.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    # Parse seed repo org/name
    if "/" in seed_repo:
        seed_org, seed_name = seed_repo.split("/", 1)
    else:
        seed_org, seed_name = "wso2", seed_repo

    seed_url = f"https://github.com/{seed_org}/{seed_name}"

    discovered: dict[str, RepoEntry] = {}
    visited: set[str] = set()

    # --- Phase 1: Seed from product repo ---
    log.info(f"Phase 1: Seeding from {seed_org}/{seed_name}" + (f" @ {tag}" if tag else ""))

    seed_dest = work_dir / seed_name
    if not clone_at_tag(seed_url, seed_dest, tag):
        log.error("Failed to clone seed repo")
        return discovered

    visited.add(seed_name)

    # Parse properties from seed POM for version resolution
    seed_properties = parse_pom_properties(seed_dest)
    log.info(f"  Parsed {len(seed_properties)} properties from seed POM")

    # P2 profile discovery
    p2_artifacts = extract_p2_features(seed_dest)
    p2_repos: set[str] = set()
    unmapped_p2: list[str] = []

    for artifact in p2_artifacts:
        entry = resolve_artifact(artifact)
        if entry and entry.name not in visited and entry.name != seed_name:
            entry.discovered_via = f"P2 profile ({artifact.group_id})"
            ver = resolve_version(artifact.version, seed_properties) if artifact.version else ""
            entry.tag = version_to_tag(ver)
            discovered[entry.name] = entry
            p2_repos.add(entry.name)
        elif not entry:
            unmapped_p2.append(f"{artifact.group_id}:{artifact.artifact_id}")

    if unmapped_p2:
        log.warning(f"  {len(unmapped_p2)} unmapped P2 artifacts:")
        for u in unmapped_p2:
            log.warning(f"    - {u}")

    log.info(f"  P2 profile: discovered {len(p2_repos)} repos")

    # Dependency discovery on seed repo (POM parsing or Maven)
    seed_dep_repos: set[str] = set()
    if use_maven:
        seed_artifacts = run_dependency_tree(seed_dest, group_prefixes, maven_timeout)
        method = "maven dependency:tree"
    else:
        seed_artifacts = parse_pom_dependencies_in_repo(
            seed_dest, group_prefixes, properties=seed_properties,
        )
        method = "POM XML parsing"

    for artifact in seed_artifacts:
        entry = resolve_artifact(artifact)
        if entry and entry.name not in visited and entry.name != seed_name:
            artifact_tag = version_to_tag(artifact.version) if artifact.version else ""
            if entry.name not in discovered:
                entry.discovered_via = f"{method} on {seed_name}"
                entry.tag = artifact_tag
                discovered[entry.name] = entry
            elif not discovered[entry.name].tag and artifact_tag:
                # Fill in tag if P2 discovery didn't have one
                discovered[entry.name].tag = artifact_tag
            seed_dep_repos.add(entry.name)
    log.info(f"  {method}: discovered {len(seed_dep_repos - p2_repos)} additional repos")

    # --- Phase 2: BFS expansion with batch cloning ---
    if max_depth > 0:
        log.info(f"Phase 2: BFS expansion (max depth {max_depth})")
    else:
        log.info("Phase 2: BFS expansion (unlimited depth)")

    # Collect initial frontier at depth 1
    current_level: list[str] = [n for n in discovered if n not in visited]
    depth = 1

    while current_level:
        if max_depth > 0 and depth > max_depth:
            log.info(f"  Reached max depth {max_depth}, stopping")
            break

        log.info(f"  [depth {depth}] Processing {len(current_level)} repos")

        # Batch clone all repos at this depth level
        to_clone = [
            (name, discovered[name].url, discovered[name].tag or None)
            for name in current_level
            if not (work_dir / name / ".git").exists()
        ]
        if to_clone:
            log.info(f"  Cloning {len(to_clone)} repos in parallel (max {clone_workers} workers)...")
            clone_results = clone_batch(to_clone, work_dir, clone_workers)
        else:
            clone_results = {}

        # Parse dependencies from all cloned repos at this level
        next_level: list[str] = []

        for repo_name in current_level:
            if repo_name in visited:
                continue
            visited.add(repo_name)

            repo_dest = work_dir / repo_name
            if not (repo_dest / ".git").exists() and not clone_results.get(repo_name, False):
                log.warning(f"    {repo_name}: clone failed, skipping")
                continue

            # Discover dependencies
            if use_maven:
                new_artifacts = run_dependency_tree(repo_dest, group_prefixes, maven_timeout)
            else:
                new_artifacts = parse_pom_dependencies_in_repo(repo_dest, group_prefixes)

            new_count = 0
            for artifact in new_artifacts:
                resolved = resolve_artifact(artifact)
                if resolved and resolved.name not in visited and resolved.name != seed_name:
                    if resolved.name not in discovered:
                        resolved.discovered_via = f"{'maven' if use_maven else 'POM parsing'} on {repo_name}"
                        discovered[resolved.name] = resolved
                        new_count += 1
                        next_level.append(resolved.name)

            if new_count > 0:
                log.info(f"    {repo_name}: found {new_count} new repos")

        current_level = [n for n in next_level if n not in visited]
        depth += 1

    return discovered


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_repos_yaml(repos: dict[str, RepoEntry], output_path: Path):
    """Write repos.yaml in the format expected by orchestrator.py."""
    data = {
        "repos": [
            {
                "name": entry.name,
                "url": entry.url,
                "shallow": entry.shallow,
                **({"tag": entry.tag} if entry.tag else {}),
            }
            for entry in sorted(repos.values(), key=lambda e: e.name)
        ]
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    log.info(f"Wrote {len(repos)} repos to {output_path}")


def print_summary(repos: dict[str, RepoEntry]):
    """Print a human-readable summary of discovered repos."""
    print(f"\n{'='*70}")
    print(f"  Discovered {len(repos)} repositories")
    print(f"{'='*70}")

    # Group by org
    by_org: dict[str, list[RepoEntry]] = {}
    for entry in sorted(repos.values(), key=lambda e: e.name):
        by_org.setdefault(entry.org, []).append(entry)

    for org, entries in sorted(by_org.items()):
        print(f"\n  {org}/ ({len(entries)} repos)")
        for entry in entries:
            via = f"  <- {entry.discovered_via}" if entry.discovered_via else ""
            tag_info = f" @ {entry.tag}" if entry.tag else ""
            print(f"    {entry.name}{tag_info}{via}")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Recursively discover WSO2 GitHub repos composing a product release"
    )
    parser.add_argument(
        "repo",
        help="GitHub repo (org/name, e.g. wso2/product-apim)",
    )
    parser.add_argument(
        "--tag",
        help="Release tag to check out (e.g. v4.3.0)",
    )
    parser.add_argument(
        "--groups",
        default="org.wso2,org.apache.synapse",
        help="Comma-separated groupId prefixes (default: org.wso2,org.apache.synapse)",
    )
    parser.add_argument(
        "--output",
        default="./resolved-repos.yaml",
        help="Output YAML path (default: ./resolved-repos.yaml)",
    )
    parser.add_argument(
        "--work-dir",
        default="./workspace",
        help="Working directory for clones (default: ./workspace)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=3,
        help="Max BFS depth, 0=unlimited (default: 3)",
    )
    parser.add_argument(
        "--use-maven",
        action="store_true",
        help="Use mvn dependency:tree instead of fast POM XML parsing (slower but finds transitive deps)",
    )
    parser.add_argument(
        "--maven-timeout",
        type=int,
        default=600,
        help="Timeout in seconds for each mvn dependency:tree call (default: 600, only with --use-maven)",
    )
    parser.add_argument(
        "--clone-workers",
        type=int,
        default=8,
        help="Max parallel git clone workers (default: 8)",
    )

    args = parser.parse_args()
    group_prefixes = [g.strip() for g in args.groups.split(",")]

    repos = discover_repos(
        seed_repo=args.repo,
        tag=args.tag,
        group_prefixes=group_prefixes,
        work_dir=Path(args.work_dir),
        max_depth=args.max_depth,
        use_maven=args.use_maven,
        maven_timeout=args.maven_timeout,
        clone_workers=args.clone_workers,
    )

    if not repos:
        log.error("No repos discovered")
        return

    print_summary(repos)
    write_repos_yaml(repos, Path(args.output))

    print(f"Next steps:")
    print(f"  # Review the output")
    print(f"  cat {args.output}")
    print(f"  # Use with orchestrator (copy to multi-repo-scanning-poc/)")
    print(f"  cp {args.output} multi-repo-scanning-poc/repos.yaml")
    print(f"  cd multi-repo-scanning-poc && uv run orchestrator.py clone")


if __name__ == "__main__":
    main()
