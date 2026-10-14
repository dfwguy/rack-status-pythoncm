---
diataxis: how-to
description: Export the deployment tracker as a static page and publish it where the cluster cannot be reached.
---

# How to publish the deployment tracker

Put a snapshot of the bring-up on a page that people outside the cluster can open, such as a
Linkify site. The page is self-contained: the data is written into it when you export it, and it
never calls back to Rack Status.

## 1. Mark the scalable units that have been handed over

A handed-over SU is counted as **Handed over**, not as completed or in remediation, and leaves the
validation-step totals. Skip this step if nothing has been handed over yet.

1. Open **Platform › Projects & Clusters** and edit the cluster.
2. Under **Scalable Unit translation**, set **Handed over** on each SU that the customer now has.
3. Save.

## 2. Check the tracker in Rack Status

Open **Analytics & Benchmarks › Deployment Tracker**. It refreshes every minute. If a banner says
Stage 2 verdicts are still loading, wait a minute and choose **Refresh** before you export, or
nodes that have run Stage 2 will show as not started.

## 3. Export the static page

Choose **Download static page**. You get `deployment-tracker.html`: one file holding the page,
its styles and fonts, and today's data. It opens from disk, so you can also attach it to a mail.

The same file is served at `GET /api/deployment-tracker/site.html` for scripts. It needs the
`report.tracker:read` action, as the in-app view does.

## 4. Upload it

On Linkify, open the site, choose **Deploy**, and drop the file. Each upload is a new deploy, so a
bad one can be rolled back by promoting the previous deploy.

Restrict the site to the people who should see this customer's fleet. Linkify defaults new sites
to all NVIDIA employees.

## What the page counts

The state definitions are printed at the bottom of the page, from the code that applies them.
Two choices are worth knowing before you show the page to anyone:

- **Remediation is readiness now.** A node that passed both stages and is drained in Slurm today
  counts as in remediation, not completed.
- **Hosts in no rack are not counted.** They are named at the bottom of the page instead. On a
  cluster with racks, that is usually the app server.
