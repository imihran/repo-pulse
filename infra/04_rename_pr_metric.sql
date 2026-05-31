-- 04_rename_pr_metric.sql
-- Rename pr_avg_merge_hours → pr_median_merge_hours.
-- The average is easily wrecked by a single stale PR merged in a quiet week;
-- the median is robust to those outliers.
ALTER TABLE repo_metrics_daily
    RENAME COLUMN pr_avg_merge_hours TO pr_median_merge_hours;
