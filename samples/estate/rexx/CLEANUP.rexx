/* REXX */
/* CLEANUP - trims the payroll output dataset and reruns the report. */
ADDRESS TSO
"ALLOC F(PAYIN) DA('PROD.PAY.OUT') SHR REUSE"
"EXECIO * DISKR PAYIN (STEM line. FINIS"
"FREE F(PAYIN)"
say "Read" line.0 "records from PROD.PAY.OUT"
CALL PAYRPT
exit 0
