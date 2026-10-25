# Oracle Always Free Charge-Avoidance Runbook

Oracle Always Free can host the outpost runner at zero cost, but only while
every resource stays inside the free envelope and the tenancy is never
upgraded. This runbook is the checklist before provisioning, the trap list to
review monthly, and the teardown procedure for retiring the host.

**Do not upgrade to Pay-As-You-Go.** The upgrade is one-way for this purpose:
trial credits stop applying, overages become invoices, and there is no path
back to a pure Always Free tenancy. If a workload does not fit the envelope,
reduce the workload, not the guardrail.

## Always Free envelope

Treat these as ceilings, not per-resource allowances. Verify the current
numbers in the OCI console before provisioning; Oracle has changed them before,
and only the console and the limits API are authoritative.

| Resource | Always Free ceiling | Notes |
|---|---|---|
| AMD compute | 2 x `VM.Standard.E2.1.Micro` | Each is 1/8 OCPU and 1 GB RAM. A third micro instance bills. |
| Arm compute | 4 OCPU / 24 GB total across Ampere A1 | One `VM.Standard.A1.Flex` at 4/24, or several smaller ones that add up to the same totals. Capacity is often unavailable; retry rather than switching to a paid shape. |
| Block storage | 200 GB total | Boot volumes, block volumes, and their backups all count toward the same 200 GB. |
| Object Storage | 10 GB | Standard object storage. Requests and excess storage bill. |
| Outbound data transfer | 10 TB per month | Egress beyond the allowance bills per GB. |
| Load balancer | 1 flexible load balancer at 10 Mbps | A second balancer, or a higher bandwidth shape, bills hourly. |
| Public IPv4 | Addresses attached to a running Always Free instance | Unattached reserved addresses and extra reserved addresses can bill. |
| Home region | Resources in the tenancy home region | Always Free does not follow you to another region. |

Recommended posture for the outpost: one AMD micro (or one small A1 slice) with
the default boot volume, one VNIC, one public IP, and no NAT gateway. The
runner validates Telegram tokens; it does not need a database, a load balancer,
or a private network path to Redis.

## Charge traps

| Trap | Why it bills | Avoid |
|---|---|---|
| Pay-As-You-Go upgrade | Removes the Always Free safety net and makes every overage an invoice. It cannot be undone. | **Do not upgrade.** Stay on the Always Free plan even when the trial ends. |
| NAT gateway | Billed hourly plus data processing, and not part of the Always Free compute allowance. | Use an internet gateway and a public IP for the single outpost, or route through the existing tunnel. |
| Extra load balancer or bigger shape | Only one 10 Mbps flexible load balancer is free. A second one, or more bandwidth, bills hourly. | Run one VM with a public IP. Do not add a balancer for a single runner. |
| Extra VNIC | Additional VNICs beyond the included count bill. | Keep one VNIC per instance. |
| Unattached reserved public IP | An address with no running instance behind it can bill, and addresses beyond the allowance bill. | Release addresses when an instance is stopped or terminated. |
| Oversized boot volume | Boot volumes count toward the 200 GB block total. Expanding a default ~47 GB boot volume to 200 GB leaves nothing for a second VM. | Keep default boot sizes. Do not resize upward. |
| Extra block volumes and backups | Every block volume and volume backup counts toward 200 GB. | Delete test volumes and old backups. Keep at most the included backup count. |
| More than 10 GB in Object Storage | Storage above the allowance bills, and so do request spikes. | Keep runner logs local and rotated; do not archive token data to a bucket. |
| Egress above 10 TB/month | Per-GB charge after the allowance. | Normal runner traffic is tiny. Investigate any unexpected egress before it grows. |
| Third AMD micro or A1 above 4/24 | The shape is outside the Always Free totals. | Stop at the envelope; do not add capacity for a burst. |
| Cross-region resources | Always Free applies in the home region only. | Keep the compartment and all resources in the home region. |
| Backup and snapshot sprawl | Backups larger than the included allowance bill. | Prune snapshots monthly. |

Two non-billing risks matter too. Idle Always Free compute can be reclaimed
after a long period without use, so keep the runner active or expect to
recreate the VM. And A1 capacity failures are an availability problem, not a
reason to move to a paid shape.

## Guardrails

1. **Dedicated compartment.** Create one compartment for this workload and put
   every instance, volume, IP, and bucket in it. Never place Always Free
   resources in the root compartment or beside unrelated paid resources.

   ```bash
   oci iam compartment create \
     --compartment-id <TENANCY_OCID> \
     --name prawn-always-free \
     --description "Always Free outpost only"
   ```

2. **Budget alert at the lowest meaningful amount.** Oracle budgets are
   tenancy-scoped targets with alert rules. Set the amount to 1 and alarm at
   the smallest threshold the console allows so any spend at all triggers a
   notification. A budget is an alert, not a hard cap, so it does not stop
   charges by itself.

   ```bash
   oci budgets budget create \
     --compartment-id <TENANCY_OCID> \
     --display-name "always-free-zero-spend" \
     --amount 1 \
     --reset-period MONTHLY \
     --target-type COMPARTMENT \
     --targets '["<COMPARTMENT_OCID>"]'
   ```

   Add an alert rule to the returned budget OCID, for example at 1 percent
   actual spend, and send it to an address you actually read.

3. **Monthly reconciliation.** Once a month, list every resource in the
   compartment, compare it to the envelope, and check the budget. Reconcile
   against the console Cost Analysis page when a number looks wrong. Never
   assume a resource is free because it was free last month.

4. **No upgrade.** Do not accept the console prompt to upgrade to
   Pay-As-You-Go, do not add a payment method to raise limits, and do not
   convert the tenancy to a paid plan for a temporary need.

5. **Tag and label.** Apply a `workload=outpost` freeform tag to every
   resource so sweeps and teardown are mechanical.

6. **Keep the runner stateless.** Logs belong on the VM with rotation. Do not
   create a database, a block volume farm, or an object bucket for runner
   state.

## Trial to Always Free

A new tenancy starts with trial credits and a time limit, usually 30 days and
US$300. Always Free resources are free during the trial as well. What happens
at the end depends on one decision:

- **Stay on Always Free** (the correct choice here). The tenancy keeps running
  Always Free eligible resources. Resources outside the envelope are stopped
  or reclaimed after a short grace period, and their data may be deleted.
  Expect to recreate anything that was not inside the envelope.
- **Upgrade to Pay-As-You-Go** (do not do this). Trial credits no longer
  apply. Every resource, including the ones that were free, is billed at list
  price. This is the single most expensive mistake in this runbook.

Before the trial ends, move the outpost onto an eligible shape and delete
anything that is not in the envelope. Then confirm the budget alert is armed
and that no paid resource remains. After the trial, check the tenancy page
periodically: Oracle may reclaim a tenancy that stays idle for a long time.

## Teardown

Stop the runner first, then remove resources in dependency order. Keep the
budget and compartment until the very end.

```bash
# 1. Stop the runner on the outpost (systemd example)
sudo systemctl disable --now outpost-runner

# 2. Delete load balancers, if any were created
oci lb load-balancer list --compartment-id <COMPARTMENT_OCID> --output table
oci lb load-balancer delete --load-balancer-id <LB_OCID> --force

# 3. Delete NAT gateways
oci network nat-gateway list --compartment-id <COMPARTMENT_OCID> --output table
oci network nat-gateway delete --nat-gateway-id <NAT_GATEWAY_OCID> --force

# 4. Release reserved public IPs
oci network public-ip list --compartment-id <COMPARTMENT_OCID> --scope REGION --output table
oci network public-ip delete --public-ip-id <PUBLIC_IP_OCID> --force

# 5. Terminate instances
oci compute instance list --compartment-id <COMPARTMENT_OCID> --output table
oci compute instance terminate --instance-id <INSTANCE_OCID> --force

# 6. Delete leftover block and boot volumes (after instances are gone)
oci bv volume list --compartment-id <COMPARTMENT_OCID> --output table
oci bv volume delete --volume-id <VOLUME_OCID> --force

# 7. Empty and delete Object Storage buckets
oci os ns get
oci os object bulk-delete --namespace <NAMESPACE> --bucket-name <BUCKET> --force
oci os bucket delete --namespace <NAMESPACE> --bucket-name <BUCKET> --force

# 8. Delete the budget, then the compartment (it must be empty)
oci budgets budget list --compartment-id <TENANCY_OCID> --output table
oci budgets budget delete --budget-id <BUDGET_OCID> --force
oci iam compartment delete --compartment-id <COMPARTMENT_OCID> --force
```

Run the verification commands after teardown. Every list should be empty and
the budget should be gone.

## Verification commands

Set the shell variables first so the commands copy and paste cleanly:

```bash
export TENANCY_OCID=<tenancy-ocid>
export COMPARTMENT_OCID=<compartment-ocid>
export AD=<availability-domain>
```

```bash
# CLI and identity sanity
oci --version
oci iam compartment list --compartment-id "$TENANCY_OCID" --output table

# Compute inventory and shapes
oci compute instance list --compartment-id "$COMPARTMENT_OCID" --output table
oci compute shape list --compartment-id "$COMPARTMENT_OCID" --availability-domain "$AD" --output table

# Always Free headroom for Arm and AMD. List definitions first if a limit name
# has changed, then query availability.
oci limits definition list --service-name compute --compartment-id "$TENANCY_OCID" --output table
oci limits resource-availability get \
  --service-name compute \
  --limit-name standard-a1-core-count \
  --compartment-id "$COMPARTMENT_OCID" \
  --availability-domain "$AD"
oci limits resource-availability get \
  --service-name compute \
  --limit-name standard-a1-memory-count \
  --compartment-id "$COMPARTMENT_OCID" \
  --availability-domain "$AD"

# Block storage total (boot + block volumes both count)
oci bv boot-volume list --compartment-id "$COMPARTMENT_OCID" --availability-domain "$AD" --output table
oci bv volume list --compartment-id "$COMPARTMENT_OCID" --output table
oci bv backup list --compartment-id "$COMPARTMENT_OCID" --output table

# Network spend surfaces
oci network public-ip list --compartment-id "$COMPARTMENT_OCID" --scope REGION --output table
oci network nat-gateway list --compartment-id "$COMPARTMENT_OCID" --output table
oci lb load-balancer list --compartment-id "$COMPARTMENT_OCID" --output table
oci network vnic list --compartment-id "$COMPARTMENT_OCID" --output table

# Object Storage usage
oci os ns get
oci os object list --namespace <NAMESPACE> --bucket-name <BUCKET> --output table

# Budgets and alerts
oci budgets budget list --compartment-id "$TENANCY_OCID" --output table
oci budgets alert-rule list --budget-id <BUDGET_OCID> --output table
```

Reconciliation rule: if a listing shows a resource that is not in the envelope
table, delete it first and ask why it existed second. If the budget shows any
spend, stop, open Cost Analysis, and identify the line item before doing
anything else. Do not upgrade to Pay-As-You-Go to "fix" a limit problem.
