# TheprawnHunter - Fix Supabase Auto-Pause Issue

## Summary
Successfully implemented a fix for Supabase auto-pause issue. Created a dedicated keepalive table and updated GitHub Actions workflow to prevent the 7-day inactivity pause.

## Changes Made

### 1. Created Migration Script
**File**: `database/migrations/003_add_keepalive_table.sql`

- Created `keepalive_logs` table for lightweight keepalive pings
- Enabled RLS with policy allowing anonymous access
- Added grants for anon role to perform SELECT and INSERT operations

### 2. Updated GitHub Actions Workflow
**File**: `.github/workflows/supabase-keep-alive.yml`

Changes:
- **Cron schedule**: Changed from `0 8 */5 * *` (every 5 days) to `0 8 */3 * *` (every 3 days)
- **Credentials**: Now uses `NEXT_PUBLIC_SUPABASE_ANON_KEY` instead of `SUPABASE_SERVICE_ROLE_KEY`
- **Target table**: Queries `keepalive_logs` instead of `discovered_credentials`
- **Enhanced activity**: Performs both SELECT and INSERT queries for better activity tracking
- **Better error handling**: More detailed status reporting and error messages
- **Timeout protection**: Added 5-minute timeout for the workflow

### 3. Updated Environment Documentation
**File**: `.env.template`

Added clear documentation for GitHub Actions secrets:
- `NEXT_PUBLIC_SUPABASE_URL`
- `NEXT_PUBLIC_SUPABASE_KEY`

## Setup Instructions

### Step 1: Run Database Migration

Go to your Supabase Dashboard:
1. Navigate to **SQL Editor**
2. Open a new query tab
3. Copy and paste the content of `database/migrations/003_add_keepalive_table.sql`
4. Execute the query
5. Verify the table was created:
   ```sql
   SELECT * FROM keepalive_logs;
   ```

### Step 2: Configure GitHub Secrets

Go to your GitHub repository:
1. Navigate to **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Add/verify the following secrets:

**Secret 1:**
- Name: `NEXT_PUBLIC_SUPABASE_URL`
- Value: Your Supabase project URL (e.g., `https://your-project.supabase.co`)

**Secret 2:**
- Name: `NEXT_PUBLIC_SUPABASE_KEY`
- Value: Your Supabase anon/public key (found in Supabase Dashboard → Project Settings → API)

⚠️ **Important**: Use the **anon** key, NOT the service_role key. The workflow explicitly requires anonymous access through the REST API gateway.

### Step 3: Verify Workflow Configuration

1. Go to **Actions** tab in your GitHub repository
2. Click on **Supabase Keep Alive** workflow
3. Verify the workflow file shows your recent changes
4. Check **Schedule** shows the cron expression: `0 8 */3 * *`

### Step 4: Test the Workflow Manually

1. In the **Supabase Keep Alive** workflow page
2. Click **Run workflow**
3. Select the main branch
4. Click **Run workflow** button
5. Wait for the workflow to complete (should take 30-60 seconds)
6. Click on the workflow run to view logs
7. Look for success message: `✅ Supabase is alive`

### Step 5: Verify Database Activity

In Supabase SQL Editor, run:
```sql
SELECT * FROM keepalive_logs ORDER BY pinged_at DESC LIMIT 5;
```

You should see a new record with:
- `source`: `github-actions`
- `pinged_at`: Recent timestamp

### Step 6: Check Next Scheduled Run

1. Go to **Actions** → **Supabase Keep Alive**
2. Scroll down to the workflow runs
3. Look for the banner showing: "Next scheduled run in X days"
4. The schedule should show approximately every 3 days

## Troubleshooting

### Workflow Fails with Auth Error (401/403)

**Problem**: Secrets are missing or incorrect
**Solution**:
1. Verify `NEXT_PUBLIC_SUPABASE_URL` is correct format (include `https://`)
2. Verify `NEXT_PUBLIC_SUPABASE_KEY` is the anon key, not service_role
3. Try testing authentication manually:
   ```bash
   curl -H "apikey: YOUR_ANON_KEY" \
        -H "Authorization: Bearer YOUR_ANON_KEY" \
        "YOUR_SUPABASE_URL/rest/v1/keepalive_logs?select=id&limit=1"
   ```

### Workflow Fails with 406 Error

**Problem**: Table or RLS policy issue
**Solution**:
1. Rerun the migration script
2. Verify RLS policy exists:
   ```sql
   SELECT * FROM pg_policies WHERE tablename = 'keepalive_logs';
   ```
3. Verify anon role has grants:
   ```sql
   SELECT grantee, privilege FROM information_schema.role_table_grants
   WHERE table_name = 'keepalive_logs' AND grantee = 'anon';
   ```

### Workflow Runs But Project Still Pauses

**Problem**: Workflow is pinging but Supabase isn't counting it as activity
**Solution**:
1. Check if workflow is actually hitting the REST API (not direct DB connection)
2. Verify both SELECT and INSERT queries are succeeding
3. Check Supabase dashboard → Monitoring → Database queries for recent activity
4. Consider moving to Pro plan if critical project

### Cron Schedule Not Working

**Problem**: GitHub Actions cron schedules have known issues
**Solution**:
1. Cron schedules: GitHub Actions may delay scheduled workflows by up to 1 hour
2. Alternative: Update cron to run more frequently (e.g., `0 8 * * 0,2,4` - Sunday, Tuesday, Thursday)
3. Use `workflow_dispatch` to manually trigger if needed
4. Check GitHub status page for Actions outages

## Why This Fix Works

### The Root Cause

1. **Wrong credentials**: Previous workflow used service_role key which may bypass Supabase's REST API gateway monitoring
2. **Inadequate table**: Querying `discovered_credentials` table may have RLS restrictions
3. **Infrequent schedule**: Every 5 days is too close to the 7-day pause threshold
4. **Single query type**: Only SELECT queries, no INSERT for better activity proof

### What Changed

1. **Correct authentication**: Uses anon key through REST API gateway, which is monitored for activity
2. **Dedicated table**: `keepalive_logs` specifically designed for anonymous keepalive pings
3. **Optimal schedule**: Every 3 days provides 4-day buffer from 7-day pause threshold
4. **Dual operations**: Both SELECT and INSERT queries for robust activity tracking
5. **Error handling**: Better diagnostics to catch issues quickly

### What Counts as Activity

According to Supabase and external guides <kreference index="1" link="https://shadhujan.medium.com/how-to-keep-supabase-free-tier-projects-active-d60fd4a17263">[^1]</kreference>:
- ✅ **Counts**: REST API queries (SELECT, INSERT, UPDATE, DELETE)
- ✅ **Counts**: Table scans and data operations
- ❌ **Doesn't count**: Just visiting your website URL
- ❌ **Doesn't count**: Static page loads
- ❌ **Doesn't count**: HTTP Health checks without DB queries

## Monitoring

After implementing the fix:

### Week 1
- Verify workflow runs manually at least once
- Check `keepalive_logs` table for records
- Confirm no auth errors in workflow logs

### Week 2-3
- Monitor automatic workflow runs via schedule
- Verify records are being added every ~3 days
- Check Supabase dashboard for pause warnings

### Week 4+
- Project should remain active indefinitely
- Monitor workflow execution history for patterns
- Consider optimizing frequency if needed

## Maintenance

### Optional: Clean Up Old Records

The `keepalive_logs` table will grow very slowly (< 1KB per ping ≈ 120 records/year). You can optionally create a cleanup job:

**As a Supabase Edge Function**:
```typescript
// Create cleanup job runs monthly
const { data, error } = await supabase
  .from('keepalive_logs')
  .delete()
  .lt('pinged_at', new Date(Date.now() - 30 * 24 * 60 * 60 * 1000));
```

**Or use pg_cron** (if available in your Supabase plan):
```sql
CREATE EXTENSION IF NOT EXISTS pg_cron;
SELECT cron.schedule(
  'keepalive-cleanup',
  '0 0 * * 1',  -- Every Monday at midnight UTC
  $$DELETE FROM keepalive_logs WHERE pinged_at < NOW() - INTERVAL '30 days'$$
);
```

## References

- [How to Keep Supabase Free Tier Projects Active](https://shadhujan.medium.com/how-to-keep-supabase-free-tier-projects-active-d60fd4a17263) <kreference index="1" link="https://shadhujan.medium.com/how-to-keep-supabase-free-tier-projects-active-d60fd4a17263">[^1]</kreference>
- [Prevent Supabase Free Tier Pausing with GitHub Actions](https://dev.to/jps27cse/how-to-prevent-your-supabase-project-database-from-being-paused-using-github-actions-3hel) <kreference index="2" link="https://dev.to/jps27cse/how-to-prevent-your-supabase-project-database-from-being-paused-using-github-actions-3hel">[^2]</kreference>
- [stylesnap supabase-keepalive.yml](file:../sgStyleSnap2025/.github/workflows/supabase-keepalive.yml) - Working reference implementation

## Success Criteria

✅ Workflow runs successfully every ~3 days without errors
✅ Records appear in `keepalive_logs` table regularly
✅ Supabase project remains active beyond 7-day threshold
✅ No auth/permission errors in workflow logs
✅ Manual workflow test completes successfully

## Next Steps

1. Execute the migration script in Supabase SQL Editor
2. Configure GitHub repository secrets
3. Test workflow manually
4. Monitor for automatic runs
5. Check back in 1-2 weeks to confirm project remains active

---

**Date**: 2025-05-05
**Project**: TheprawnHunter
**Impact**: Prevents automatic Supabase pause due to inactivity
