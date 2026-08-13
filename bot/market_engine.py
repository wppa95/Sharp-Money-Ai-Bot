*** Begin Patch
*** Update File: bot/market_engine.py
@@
     # New-prop immediate path
-    if _np_bet_ready and decision is not None:
-        # existing _np_bet_ready flow continues
-        pass
+    if _np_bet_ready and decision is not None:
+        # Apply MQ gate for new-prop immediate candidates
+        allowed, mq_reason = _mq_allows_action(decision, market_quality)
+        if not allowed:
+            _np_bet_ready = False
+            logger.debug(
+                "underdog_job: MQ gate blocked new-prop actionable alert — %s | %s | tier=%s | mq=%s | reason=%s",
+                player, stat_type,
+                getattr(decision, "decision_tier", None),
+                getattr(market_quality, "label", None),
+                mq_reason,
+            )
@@
     # Standing/stable actionable path
-    for _sp, _st, _line_val, _sdec, _smq in standing_candidates:
-        # existing standing processing
-        pass
+    for _sp, _st, _line_val, _sdec, _smq in standing_candidates:
+        # Apply MQ gate for standing picks
+        allowed, mq_reason = _mq_allows_action(_sdec, _smq)
+        if not allowed:
+            logger.debug(
+                "underdog_job: MQ gate blocked standing actionable alert — %s | %s | tier=%s | mq=%s | reason=%s",
+                _sp, _st, getattr(_sdec, "decision_tier", None),
+                getattr(_smq, "label", None), mq_reason,
+            )
+            continue
+        # existing standing processing continues
+        pass
*** End Patch