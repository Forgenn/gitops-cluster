#!/usr/bin/env python3
"""
Add ESO default fields to all ExternalSecret manifests so they match
what ESO injects at runtime, eliminating ArgoCD OutOfSync drift.

Fields added (ESO defaults):
  spec.refreshInterval: 1h
  spec.target.deletionPolicy: Retain
  spec.data[*].remoteRef.conversionStrategy: Default
  spec.data[*].remoteRef.decodingStrategy: None
  spec.data[*].remoteRef.metadataPolicy: None
"""
import sys
import os
import glob

# Use ruamel.yaml to preserve comments and formatting
try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
except ImportError:
    print("Installing ruamel.yaml...")
    os.system(f"{sys.executable} -m pip install ruamel.yaml -q")
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 4096  # prevent line wrapping
yaml.indent(mapping=2, sequence=4, offset=2)

infra_dir = os.path.join(os.path.dirname(__file__), '..', 'infra')

changed = []
skipped = []

for path in sorted(glob.glob(f"{infra_dir}/**/*.yaml", recursive=True)):
    with open(path) as f:
        content = f.read()

    if 'kind: ExternalSecret' not in content:
        continue

    with open(path) as f:
        docs = list(yaml.load_all(f))

    modified = False
    new_docs = []

    for doc in docs:
        if not isinstance(doc, dict):
            new_docs.append(doc)
            continue
        if doc.get('kind') != 'ExternalSecret':
            new_docs.append(doc)
            continue

        spec = doc.get('spec', {})

        # 1. refreshInterval
        if 'refreshInterval' not in spec:
            spec['refreshInterval'] = '1h'
            modified = True

        # 2. target.deletionPolicy
        target = spec.get('target', {})
        if target is not None and 'deletionPolicy' not in target:
            target['deletionPolicy'] = 'Retain'
            modified = True

        # 3. data[*].remoteRef defaults
        for entry in spec.get('data') or []:
            remote_ref = entry.get('remoteRef')
            if remote_ref is None:
                continue
            for field, default in [
                ('conversionStrategy', 'Default'),
                ('decodingStrategy', 'None'),
                ('metadataPolicy', 'None'),
            ]:
                if field not in remote_ref:
                    remote_ref[field] = default
                    modified = True

        # 4. dataFrom[*].extract defaults (less common but same pattern)
        for entry in spec.get('dataFrom') or []:
            extract = entry.get('extract')
            if extract is None:
                continue
            for field, default in [
                ('conversionStrategy', 'Default'),
                ('decodingStrategy', 'None'),
                ('metadataPolicy', 'None'),
            ]:
                if field not in extract:
                    extract[field] = default
                    modified = True

        new_docs.append(doc)

    if modified:
        import io
        buf = io.StringIO()
        yaml.dump_all(new_docs, buf)
        with open(path, 'w') as f:
            f.write(buf.getvalue())
        changed.append(path.replace(infra_dir + os.sep, ''))
    else:
        skipped.append(path.replace(infra_dir + os.sep, ''))

print(f"\nModified {len(changed)} files:")
for p in changed:
    print(f"  {p}")

print(f"\nSkipped {len(skipped)} (already correct):")
for p in skipped:
    print(f"  {p}")
