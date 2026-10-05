#!/usr/bin/env bash
set -euo pipefail

action=${1:?action is required}
su_csv=${2:?SU selection is required}
image_csv=${3:-}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
eligible=$root/inventory/SU1-SU14-live-eligible-3951.nodes
stamp=$(date -u +%Y%m%dT%H%M%SZ)

declare -A allowed_images=()
while IFS=$'\t' read -r order display filename bytes sha; do
  [[ $order == order ]] && continue
  allowed_images[$filename]=1
done < "$root/catalog/images.tsv"

IFS=, read -ra sus <<< "$su_csv"
IFS=, read -ra images <<< "$image_csv"
(( ${#sus[@]} > 0 ))
temporary=$(mktemp -d "$root/state/.catalog-operation.XXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
requested=$temporary/requested.nodes

for su in "${sus[@]}"; do
  [[ $su =~ ^([1-9]|1[0-4])$ ]] || { echo "INVALID_SU value=$su" >&2; exit 64; }
  cat "$root/inventory/SU${su}-288.nodes" >> "$requested"
done
sort -u "$requested" -o "$requested"
roster=$temporary/eligible.nodes
comm -12 "$eligible" "$requested" > "$roster"
[[ -s $roster ]] || { echo "EMPTY_ELIGIBLE_ROSTER" >&2; exit 65; }

for image in "${images[@]}"; do
  [[ -z $image || ${allowed_images[$image]:-0} == 1 ]] || { echo "INVALID_IMAGE value=$image" >&2; exit 64; }
done

case "$action" in
  preflight)
    NVIS_P2P_PREFLIGHT_PARALLELISM=64 "$root/scripts/preflight-catalog-fleet.sh" "$requested" "console-SU${su_csv//,/-}"
    ;;
  deploy)
    NVIS_P2P_DEPLOY_PARALLELISM=48 "$root/scripts/deploy-catalog-peer-fleet.sh" "$roster" "console-SU${su_csv//,/-}"
    ;;
  persist)
    (( ${#images[@]} > 0 )) || { echo "NO_IMAGES_SELECTED" >&2; exit 64; }
    base=$root/deploy/object-store
    client=$root/dist/client-amd64-v1.4.9
    secret=$(tr -d '\n' < "$base/secrets/dfstore_password")
    export DFSTORE_IMPORT_STORAGE_ACCESS_KEY_ID=nvis-p2p-client
    export DFSTORE_IMPORT_STORAGE_ACCESS_KEY_SECRET="$secret"
    export DFSTORE_IMPORT_STORAGE_ENDPOINT=https://10.41.112.3:19000
    export DFSTORE_IMPORT_STORAGE_INSECURE_SKIP_VERIFY=true
    export DFSTORE_IMPORT_STORAGE_REGION=us-east-1
    mkdir -p "$root/state/persistence"
    for image in "${images[@]}"; do
      row=$(awk -F '\t' -v name="$image" '$3 == name {print; exit}' "$root/catalog/images.tsv")
      IFS=$'\t' read -r order display filename bytes sha <<< "$row"
      "$client/dfstore" import \
        --endpoint "$root/state/seed/dfdaemon.sock" \
        --persistent-replica-count 28 --ttl 7d --timeout 6h --no-progress --console \
        "/cluster/containers/$filename" \
        --url "s3://nvis-p2p-artifacts/images/$sha/$filename"
      printf '%s\t%s\t%s\t%s\n' "$filename" "$sha" "$(date -u +%FT%TZ)" 28 > "$root/state/persistence/$filename.tsv"
      echo "PERSIST_READY filename=$filename sha256=$sha replicas=28 ttl=7d"
    done
    ;;
  sync)
    NVIS_P2P_SYNC_PARALLELISM=48 \
      "$root/scripts/sync-catalog-service-fleet.sh" \
      "$roster" "console-SU${su_csv//,/-}"
    ;;
  distribute|repair)
    (( ${#images[@]} == 1 )) || { echo "SELECT_EXACTLY_ONE_IMAGE" >&2; exit 64; }
    NVIS_P2P_ROLLOUT_PARALLELISM=48 \
      "$root/scripts/rollout-catalog-image-fleet.sh" \
      "${images[0]}" "$roster" "console-SU${su_csv//,/-}"
    ;;
  verify)
    output=$root/state/catalog-verify-console-SU${su_csv//,/-}-$stamp.tsv
    printf 'node\tstate\tunverified\tmissing\tbad_size\tactive\n' > "$output"
    xargs -r -P 64 -n 1 "$root/scripts/verify-catalog-node.sh" < "$roster" >> "$output"
    echo "VERIFY_COMPLETE nodes=$(tail -n +2 "$output" | wc -l) output=$output"
    ;;
  *) echo "INVALID_ACTION value=$action" >&2; exit 64 ;;
esac
