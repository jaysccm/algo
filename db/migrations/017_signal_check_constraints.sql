-- 017_signal_check_constraints.sql (mirror of db/init/041)
--
-- Vocabulary CHECKs for news_signals and macro_signals (JAY-169; the
-- "DB CHECK constraint" follow-up in v2/BUGS.md under P0.3). The classifier
-- coerces these values now; the constraints stop anything else from reaching
-- the tables.
--
-- NOT VALID: long-lived volumes hold legacy rows outside the vocabulary
-- (sentiment 'mixed', categories 'news', 'corporate', 'financial', ...).
-- NOT VALID enforces the rule on every new or updated row and leaves old rows
-- alone, so no data is rewritten. NULLs pass a CHECK, as the columns allow NULL.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'news_signals_category_check'
                   AND conrelid = 'news_signals'::regclass) THEN
        ALTER TABLE news_signals ADD CONSTRAINT news_signals_category_check
            CHECK (category IN ('earnings', 'guidance', 'analyst', 'product', 'legal', 'noise')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'news_signals_sentiment_check'
                   AND conrelid = 'news_signals'::regclass) THEN
        ALTER TABLE news_signals ADD CONSTRAINT news_signals_sentiment_check
            CHECK (sentiment IN ('bullish', 'bearish', 'neutral')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'news_signals_confidence_check'
                   AND conrelid = 'news_signals'::regclass) THEN
        ALTER TABLE news_signals ADD CONSTRAINT news_signals_confidence_check
            CHECK (confidence IN ('high', 'medium', 'low')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'macro_signals_category_check'
                   AND conrelid = 'macro_signals'::regclass) THEN
        ALTER TABLE macro_signals ADD CONSTRAINT macro_signals_category_check
            CHECK (category IN ('fed', 'trade', 'regulation', 'geopolitical', 'fiscal', 'election', 'sector')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'macro_signals_sentiment_check'
                   AND conrelid = 'macro_signals'::regclass) THEN
        ALTER TABLE macro_signals ADD CONSTRAINT macro_signals_sentiment_check
            CHECK (sentiment IN ('bullish', 'bearish', 'neutral')) NOT VALID;
    END IF;
END $$;
