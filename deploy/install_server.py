#!/usr/bin/env python3
"""Install the restricted helper through existing administrator SSH access.

The new CI key only runs this helper, with forwarding, PTY and user rc disabled.
It cannot replace the helper/allowlist. Existing SSH keys are preserved.
"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import tempfile
from catalog import ROOT


def run(args, *, input=None, okay=(0,)):
    p = subprocess.run(args, input=input, capture_output=True)
    if p.returncode not in okay:
        raise RuntimeError("Installation step failed: " + args[0] + " (sensitive details suppressed)")
    return p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="zendesk-agent-server")
    parser.add_argument("--address", default="150.109.239.157")
    parser.add_argument("--repo", default="david-yang-pixel/mcp-plugins")
    parser.add_argument("--update-helper-only", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mcp-ci-key-") as folder:
        key = Path(folder) / "identity"
        public = None
        if not args.update_helper_only:
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "mcp-plugins-ci", "-f", str(key)])
            public = key.with_suffix(".pub").read_text().strip()
        payload = {"helper": base64.b64encode((ROOT / "deploy/server.py").read_bytes()).decode(),
                   "targets": base64.b64encode((ROOT / "deploy/targets.json").read_bytes()).decode(), "public_key": public}
        script = '''import base64,json,pathlib,os,shutil,time,hashlib,fcntl
os.umask(0o077)
lock=open('/run/lock/mcp-plugins-deploy.lock','w')
fcntl.flock(lock,fcntl.LOCK_EX)
data=json.loads(PAYLOAD)
stamp=str(time.time_ns())
authorized=pathlib.Path('/root/.ssh/authorized_keys')
existing=authorized.read_text() if authorized.exists() else ''
if data['public_key'] and any(line.rstrip().endswith(' mcp-plugins-ci') for line in existing.splitlines()):
 raise RuntimeError('CI key already exists; use --update-helper-only')
for folder in ['/etc/mcp-plugins','/opt/mcp-plugins','/opt/mcp-plugins/releases','/opt/mcp-plugins/state']:
 p=pathlib.Path(folder);p.mkdir(mode=0o700,exist_ok=True);p.chmod(0o700)
candidate=base64.b64decode(data['helper'])
namespace={'__name__':'mcp_admin_install','__file__':'/usr/local/sbin/mcp-plugins-deploy'}
exec(compile(candidate,namespace['__file__'],'exec'),namespace)
# Backfill before replacing targets: the old helper accepts this same old
# contract if installation stops early, while the new allowlist cannot recover it.
previous_targets=pathlib.Path('/etc/mcp-plugins/targets.json')
old_targets=json.loads(previous_targets.read_text()) if previous_targets.exists() else {}
new_targets=json.loads(base64.b64decode(data['targets']))
namespace['record_prior_legacy_contracts'](old_targets,new_targets)
nginx_result=namespace['install_nginx_include']()
for target,field,mode in [('/usr/local/sbin/mcp-plugins-deploy','helper',0o700),('/etc/mcp-plugins/targets.json','targets',0o600)]:
 p=pathlib.Path(target)
 if p.exists():shutil.copy2(p,str(p)+'.bak-'+stamp)
 content=base64.b64decode(data[field])
 if field=='helper':compile(content,target,'exec')
 else:json.loads(content)
 temp=p.with_name(p.name+'.new');temp.write_bytes(content);temp.chmod(mode);os.replace(temp,p)
if data['public_key']:
 if authorized.exists():shutil.copy2(authorized,str(authorized)+'.bak-mcp-'+stamp)
 authorized.parent.mkdir(mode=0o700,exist_ok=True)
 line='restrict,command="/usr/local/sbin/mcp-plugins-deploy" '+data['public_key']+'\\n'
 authorized.write_text(existing.rstrip()+'\\n'+line);authorized.chmod(0o600)
print(json.dumps({'host_key':pathlib.Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip(),'helper_sha256':hashlib.sha256(pathlib.Path('/usr/local/sbin/mcp-plugins-deploy').read_bytes()).hexdigest(),'nginx':nginx_result}))
'''.replace("PAYLOAD", repr(json.dumps(payload)))
        result = json.loads(run(["ssh", "-o", "BatchMode=yes", args.host, "python3 -"], input=script.encode()).stdout)
        if not args.update_helper_only:
            known = Path(folder) / "known_hosts"
            known.write_text(args.address + " " + " ".join(result["host_key"].split()[:2]) + "\n")
            result_probe = run(["ssh", "-T", "-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known), "root@" + args.address, "id"], okay=(1,))
            if b"Only deployment of a valid service" not in result_probe.stderr:
                raise RuntimeError("Restricted SSH refusal was not verified")
            run(["gh", "secret", "set", "MCP_DEPLOY_SSH_KEY", "--repo", args.repo], input=key.read_bytes())
            run(["gh", "variable", "set", "MCP_DEPLOY_HOST", "--repo", args.repo, "--body", args.address])
            run(["gh", "variable", "set", "MCP_DEPLOY_KNOWN_HOSTS", "--repo", args.repo, "--body", known.read_text()])
            print("Dedicated CI key stored; verified it cannot run arbitrary SSH commands")
        print("Installed helper SHA-256:", result["helper_sha256"])
        print("Managed Nginx include:", "installed with graceful reload" if result['nginx']['changed'] else "already installed")


if __name__ == "__main__":
    main()
