#!/usr/bin/env bash
# Tear everything down.
#
# Deleting the kind cluster removes the application, PostgreSQL, its PersistentVolumeClaim and
# LocalStack in one action, because all of it runs inside the cluster. Terraform state is removed
# too, since the LocalStack instance it described no longer exists.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if ! cluster_exists; then
  step "No kind cluster named '$CLUSTER_NAME'; nothing to tear down"
else
  # Best effort: if LocalStack is already gone this fails harmlessly, and the cluster deletion
  # below removes everything regardless.
  if have terraform && [ -d "$TERRAFORM_DIR/.terraform" ]; then
    step "Destroying Terraform-managed resources"
    terraform -chdir="$TERRAFORM_DIR" destroy -auto-approve -input=false -no-color >/dev/null 2>&1 \
      && ok "terraform destroy" \
      || warn "terraform destroy did not complete (LocalStack may already be gone); continuing"
  fi

  step "Deleting kind cluster '$CLUSTER_NAME'"
  kind delete cluster --name "$CLUSTER_NAME"
  ok "cluster deleted"
fi

step "Removing local Terraform state"
# State describes a LocalStack instance that no longer exists, and the provider cache is a
# download. The lock file stays: it is committed, pins provider versions and checksums, and is
# what makes the next `make up` resolve to the same provider on any machine.
rm -f "$TERRAFORM_DIR/terraform.tfstate" \
      "$TERRAFORM_DIR/terraform.tfstate.backup" 2>/dev/null || true
rm -rf "$TERRAFORM_DIR/.terraform" 2>/dev/null || true
ok "state removed (the committed provider lock file is kept)"

printf '\n'
step "ConvoScore is down"
printf '     Your .env is untouched. Run: make up\n'
