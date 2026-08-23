-- =====================================================================
-- Idempotent data repair: delete FlexImportService sentinel orphan trades
--
-- Context
-- -------
-- `FlexImportService.import_flex` (backend/app/services/flex_import_service.py)
-- uses the legacy parser path and never creates LogicalExecution rows, so
-- every trade it creates is leg-less. It also has none of the
-- `protected-trade-rebuild-isolation` guards (no ProtectedExecutionSnapshot,
-- no protected_contracts passed to the aggregator), so the FIFO aggregator's
-- `entry_price = 0.001` sentinel-orphan mechanism fires unsuppressed for
-- closing legs of protected combo positions. See the `flex-sync-raw-pipeline`
-- follow-up change for the actual fix; this script only removes the three
-- confirmed phantom rows already on production.
--
-- Target rows (confirmed 2026-08-22, IBKR Flex sync)
--   b7125156-3ecc-4324-b625-978caae4776d  QQQ  entry_price=0.001  0 legs
--   1b9c1243-6ac6-4db0-8653-fe519de489c1  QQQ  entry_price=0.001  0 legs
--   f36c5fff-2680-4e00-afa5-3e29c45a05ad  XSP  entry_price=0.001  0 legs
--
-- Safety
-- ------
-- Every row is verified immediately before deletion to have zero linked
-- logical_executions, trade_notes, trade_annotations, and trade_taxonomies
-- rows. A row with any attached data is left untouched and reported instead
-- of being deleted, so this script can never silently discard something a
-- user attached to one of these trades after they were created.
--
-- Idempotency
-- -----------
-- The DELETE only ever removes rows matching the confirmed IDs; a re-run
-- after successful deletion matches zero rows and is a no-op.
--
-- Usage
-- -----
--   kubectl exec -n tradesensei postgres-0 -- psql -U tradesensei -d tradesensei \
--     -v ON_ERROR_STOP=1 -f - < tools/repair_flex_sentinel_orphans.sql
--
-- Runs in a single transaction: it either applies completely or not at all.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL row_security = off;

-- ---------------------------------------------------------------------
-- 0. Preconditions - verify each target row is a genuine leg-less sentinel
--    with no attached user data before touching anything
-- ---------------------------------------------------------------------
DO $$
DECLARE
    rec record;
    unsafe_count integer := 0;
BEGIN
    FOR rec IN
        SELECT t.id, t.symbol, t.entry_price,
               (SELECT count(*) FROM logical_executions le WHERE le.trade_id = t.id) AS legs,
               (SELECT count(*) FROM trade_notes tn WHERE tn.trade_id = t.id) AS notes,
               (SELECT count(*) FROM trade_annotations ta WHERE ta.trade_id = t.id) AS annots,
               (SELECT count(*) FROM trade_taxonomies tt WHERE tt.trade_id = t.id) AS tax
        FROM trades t
        WHERE t.id IN (
            'b7125156-3ecc-4324-b625-978caae4776d',
            '1b9c1243-6ac6-4db0-8653-fe519de489c1',
            'f36c5fff-2680-4e00-afa5-3e29c45a05ad'
        )
    LOOP
        IF rec.entry_price IS DISTINCT FROM 0.001
           OR rec.legs <> 0
           OR rec.notes <> 0
           OR rec.annots <> 0
           OR rec.tax <> 0
        THEN
            unsafe_count := unsafe_count + 1;
            RAISE WARNING
                'Refusing to delete %/%: entry_price=% legs=% notes=% annotations=% taxonomies=%',
                rec.id, rec.symbol, rec.entry_price, rec.legs, rec.notes, rec.annots, rec.tax;
        END IF;
    END LOOP;

    IF unsafe_count > 0 THEN
        RAISE EXCEPTION
            '% target row(s) failed the leg-less/no-attached-data check; aborting without deleting anything.',
            unsafe_count;
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- 1. Delete the confirmed sentinel orphans
-- ---------------------------------------------------------------------
DELETE FROM trades
WHERE id IN (
    'b7125156-3ecc-4324-b625-978caae4776d',
    '1b9c1243-6ac6-4db0-8653-fe519de489c1',
    'f36c5fff-2680-4e00-afa5-3e29c45a05ad'
);

-- ---------------------------------------------------------------------
-- 2. Post-condition - confirm all three are gone
-- ---------------------------------------------------------------------
DO $$
DECLARE
    remaining integer;
BEGIN
    SELECT count(*) INTO remaining
    FROM trades
    WHERE id IN (
        'b7125156-3ecc-4324-b625-978caae4776d',
        '1b9c1243-6ac6-4db0-8653-fe519de489c1',
        'f36c5fff-2680-4e00-afa5-3e29c45a05ad'
    );
    IF remaining <> 0 THEN
        RAISE EXCEPTION 'Post-condition failed: % target row(s) still present.', remaining;
    END IF;
    RAISE NOTICE 'Flex sentinel orphan repair verified successfully.';
END $$;

COMMIT;

-- ---------------------------------------------------------------------
-- Result summary
-- ---------------------------------------------------------------------
SET row_security = off;

SELECT count(*) AS remaining_sentinel_orphans_account_wide
FROM trades
WHERE entry_price = 0.001;
