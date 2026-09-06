# Broad-Except Site Inventory

Generated during 2026-09-06 remediation cycle T45. Source: git grep -nE 'except Exception:' -- 'app/**/*.py'.

Full sweep of the 156 sites is deferred to a follow-up cycle. This document is the seed inventory for that work. Each site should be tagged as one of:

- `[BEST-EFFORT]` — telemetry / audit paths that must never break the caller. Broad catch acceptable; ensure a logger.debug(...) message is emitted so failures aren't silent.
- `[MASKS-BUG]` — swallows a specific class of exception that might be a genuine bug. Replace with a narrower xcept SpecificError: and let others propagate.
- `[NEEDS-CONTEXT]` — the wrapper hides context the caller needs; consider aise ... from e or re-raise.

Cycle scope: only 20 sites are patched inline (see DEAD-008 partial). The rest ship as tagged inventory.

---

## Sites

app/api/routers/health.py:216:    except Exception:
app/api/routers/health.py:427:        except Exception:
app/api/routers/ingest.py:318:                    except Exception:
app/api/routers/media.py:31:    except Exception:
app/api/routers/media.py:54:    except Exception:
app/api/routers/media.py:63:    except Exception:
app/api/routers/monitor.py:62:            except Exception:
app/api/routers/monitor.py:64:    except Exception:
app/api/routers/monitor.py:84:    except Exception:
app/api/routers/monitor.py:897:        except Exception:
app/api/routers/scan.py:52:    except Exception:
app/core/audit.py:200:            except Exception:
app/core/circuit_breaker.py:87:                except Exception:
app/core/circuit_breaker.py:108:            except Exception:
app/core/database.py:33:    except Exception:
app/core/metrics.py:94:                except Exception:
app/core/metrics.py:110:                    except Exception:
app/core/metrics.py:199:        except Exception:
app/core/queue_monitor.py:29:        except Exception:
app/core/queue_monitor.py:42:        except Exception:
app/core/queue_monitor.py:61:        except Exception:
app/core/queue_monitor.py:139:        except Exception:
app/core/queue_monitor.py:145:        except Exception:
app/core/redis_srv.py:86:    except Exception:
app/core/redis_srv.py:112:    except Exception:
app/core/redis_srv.py:148:    except Exception:
app/core/redis_srv.py:167:    except Exception:
app/core/security.py:36:                except Exception:
app/core/webhook.py:37:    except Exception:
app/services/_scraper/lifecycle.py:61:        except Exception:
app/services/_scraper/lifecycle.py:100:        except Exception:
app/services/_scraper/monitor_guard.py:53:        except Exception:
app/services/_scraper/strategies.py:46:    except Exception:
app/services/_scraper/strategies.py:159:        except Exception:
app/services/_scraper/strategies.py:289:                            except Exception:
app/services/_scraper/strategies.py:369:            except Exception:
app/services/_scraper/strategies.py:525:            except Exception:
app/services/_scraper/strategies.py:560:                except Exception:
app/services/_scraper/strategies.py:880:                except Exception:
app/services/bot_listener.py:82:    except Exception:
app/services/bot_listener.py:202:    except Exception:
app/services/bot_listener.py:560:        except Exception:
app/services/bot_listener.py:676:                except Exception:
app/services/bot_listener.py:728:            except Exception:
app/services/bot_listener.py:1524:            except Exception:
app/services/bot_listener.py:1539:        except Exception:
app/services/bot_listener.py:1564:            except Exception:
app/services/broadcaster_srv.py:218:            except Exception:
app/services/broadcaster_srv.py:459:        except Exception:
app/services/broadcaster_srv.py:481:        except Exception:
app/services/broadcaster_srv.py:575:        except Exception: pass
app/services/finding_alerts.py:459:            except Exception:
app/services/scanners.py:109:    except Exception:
app/services/scanners.py:240:        except Exception:
app/services/scanners.py:253:    except Exception:
app/services/scanners.py:297:                except Exception: pass
app/services/scanners.py:329:                            except Exception: pass
app/services/scanners.py:420:                        except Exception:
app/services/scanners.py:503:                except Exception: pass
app/services/scanners.py:535:                            except Exception: pass
app/services/scanners.py:550:                            except Exception: pass
app/services/scanners.py:625:        except Exception:
app/services/scanners.py:714:                        except Exception:
app/services/scanners.py:794:                        except Exception:
app/services/scanners.py:988:                except Exception:
app/services/scanners.py:1120:                except Exception:
app/services/scanners.py:1135:                except Exception:
app/services/scanners.py:1226:                    except Exception:
app/services/scanners.py:1246:                        except Exception:
app/services/scanners_extension.py:74:                                except Exception: pass
app/services/scanners_extension.py:178:                        except Exception: return None
app/services/scanners_extension.py:235:                        except Exception: return None
app/services/scanners_extension.py:405:                        except Exception: return []
app/services/scanners_extension.py:458:                        except Exception:
app/services/scanners_extension.py:465:                        except Exception: pass
app/services/scanners_extension.py:530:                        except Exception:
app/services/scanners_extension.py:537:                        except Exception: pass
app/services/scanners_extension.py:799:                    except Exception:
app/services/scanners_extension.py:822:                        except Exception:
app/services/scanners_extension.py:867:        except Exception:
app/services/scanners_extension.py:875:        except Exception:
app/services/scanners_extension.py:951:                    except Exception:
app/services/scanners_extension.py:963:                    except Exception:
app/services/scraper_srv.py:107:    except Exception:
app/services/scraper_srv.py:373:            except Exception:
app/services/scraper_srv.py:436:                except Exception:
app/services/scraper_srv.py:494:                except Exception:
app/services/scraper_srv.py:911:            except Exception:
app/services/user_agent_srv.py:80:    except Exception:
app/services/user_agent_srv.py:340:        except Exception:
app/services/user_agent_srv.py:871:                except Exception: return None
app/services/user_agent_srv.py:883:            except Exception: return None
app/services/user_agent_srv.py:934:        except Exception: return False
app/services/user_agent_srv.py:955:                except Exception: pass
app/services/user_agent_srv.py:968:                except Exception: pass
app/services/user_agent_srv.py:1214:                    except Exception: pass
app/services/user_agent_srv.py:1216:            except Exception: return 0
app/services/user_agent_srv.py:1237:                        except Exception: pass
app/services/user_agent_srv.py:1239:            except Exception: return 0
app/services/user_agent_srv.py:1251:            except Exception: return None
app/utils/helpers.py:63:    except Exception:
app/workers/tasks/audit_tasks.py:47:    except Exception:
app/workers/tasks/audit_tasks.py:71:        except Exception:
app/workers/tasks/audit_tasks.py:272:        except Exception:
app/workers/tasks/audit_tasks.py:294:        except Exception:
app/workers/tasks/firehose_tasks.py:151:                        except Exception:
app/workers/tasks/flow_tasks.py:972:        except Exception:
app/workers/tasks/flow_tasks.py:1096:        except Exception:
app/workers/tasks/flow_tasks.py:1128:            except Exception:
app/workers/tasks/flow_tasks.py:1636:    except Exception:
app/workers/tasks/flow_tasks.py:1841:            except Exception:
app/workers/tasks/flow_tasks.py:1856:                except Exception:
app/workers/tasks/flow_tasks.py:1899:                except Exception:
app/workers/tasks/flow_tasks.py:1908:                except Exception:
app/workers/tasks/flow_tasks.py:1917:                except Exception:
app/workers/tasks/flow_tasks.py:2106:        except Exception:
app/workers/tasks/flow_tasks.py:2469:    except Exception:
app/workers/tasks/flow_tasks.py:2576:        except Exception:
app/workers/tasks/flow_tasks.py:2864:            except Exception:
app/workers/tasks/flow_tasks.py:2873:            except Exception:
app/workers/tasks/flow_tasks.py:3140:        except Exception:
app/workers/tasks/flow_tasks.py:3328:        except Exception:
app/workers/tasks/flow_tasks.py:3482:        except Exception:
app/workers/tasks/flow_tasks.py:3532:        except Exception:
app/workers/tasks/flow_tasks.py:3639:        except Exception:
app/workers/tasks/flow_tasks.py:3652:    except Exception:
app/workers/tasks/flow_tasks.py:3667:    except Exception:
app/workers/tasks/flow_tasks.py:3682:    except Exception:
app/workers/tasks/flow_tasks.py:3804:    except Exception:
app/workers/tasks/flow_tasks.py:3894:    except Exception:
app/workers/tasks/flow_tasks.py:3925:    except Exception:
app/workers/tasks/flow_tasks.py:4014:                        except Exception:
app/workers/tasks/flow_tasks.py:4220:        except Exception:
app/workers/tasks/flow_tasks.py:4241:                except Exception:
app/workers/tasks/flow_tasks.py:4371:        except Exception:
app/workers/tasks/flow_tasks.py:4615:    except Exception:
app/workers/tasks/flow_tasks.py:4623:        except Exception:
app/workers/tasks/flow_tasks.py:4712:    except Exception:
app/workers/tasks/flow_tasks.py:4727:    except Exception:
app/workers/tasks/honeypot_redirect_strategies.py:213:        except Exception:
app/workers/tasks/honeypot_redirect_strategies.py:225:        except Exception:
app/workers/tasks/honeypot_redirect_strategies.py:234:        except Exception:
app/workers/tasks/honeypot_redirect_strategies.py:244:        except Exception:
app/workers/tasks/honeypot_redirect_tasks.py:217:            except Exception:
app/workers/tasks/honeypot_redirect_tasks.py:241:                except Exception:
app/workers/tasks/pivot_tasks.py:70:    except Exception:
app/workers/tasks/pivot_tasks.py:83:    except Exception:
app/workers/tasks/pivot_tasks.py:196:    except Exception:
app/workers/tasks/scanner_tasks.py:81:    except Exception:
app/workers/tasks/scanner_tasks.py:109:    except Exception:
app/workers/tasks/scanner_tasks.py:1186:            except Exception: pass
app/workers/tasks/validation_tasks.py:186:                except Exception:
app/workers/tasks/validation_tasks.py:210:            except Exception:
app/workers/tasks/validation_tasks.py:437:    except Exception:
app/workers/tasks/validation_tasks.py:447:        except Exception:
app/workers/tasks/validation_tasks.py:624:    except Exception:
