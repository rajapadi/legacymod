       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYCALC.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT EMP-FILE ASSIGN TO EMPMAST
               ORGANIZATION IS INDEXED ACCESS IS SEQUENTIAL
               RECORD KEY IS EMP-ID.
           SELECT PAY-FILE ASSIGN TO PAYOUT.
       DATA DIVISION.
       FILE SECTION.
       FD  EMP-FILE.
       COPY EMPREC.
       FD  PAY-FILE.
       01  PAY-RECORD.
           05  PAY-EMP-ID        PIC X(6).
           05  PAY-GROSS         PIC S9(7)V99 COMP-3.
           05  PAY-STATUS        PIC X.
       WORKING-STORAGE SECTION.
       01  WS-HOURS              PIC 9(3)V99.
       01  WS-RATE               PIC 9(4)V99.
       01  WS-GROSS              PIC S9(7)V99 COMP-3.
       01  WS-EOF                PIC X VALUE 'N'.
           88  END-OF-FILE       VALUE 'Y'.
       PROCEDURE DIVISION.
       MAIN-PARA.
           OPEN INPUT EMP-FILE OUTPUT PAY-FILE.
           PERFORM READ-EMP UNTIL END-OF-FILE.
           CLOSE EMP-FILE PAY-FILE.
           GOBACK.
       READ-EMP.
           READ EMP-FILE AT END MOVE 'Y' TO WS-EOF
               NOT AT END PERFORM CALC-PAY
           END-READ.
       CALC-PAY.
           MOVE EMP-HOURS TO WS-HOURS.
           MOVE EMP-RATE  TO WS-RATE.
           IF WS-HOURS GREATER THAN 40
               COMPUTE WS-GROSS = (40 * WS-RATE)
                   + ((WS-HOURS - 40) * WS-RATE * 1.5)
           ELSE
               COMPUTE WS-GROSS = WS-HOURS * WS-RATE
           END-IF.
           MOVE EMP-ID TO PAY-EMP-ID.
           MOVE WS-GROSS TO PAY-GROSS.
           IF WS-GROSS GREATER THAN 9999.99
               MOVE 'H' TO PAY-STATUS
               CALL 'PAYAUDIT' USING PAY-RECORD
               EXEC SQL
                   INSERT INTO PAYROLL_AUDIT
                          (EMP_ID, GROSS, AUDIT_TS)
                   VALUES (:PAY-EMP-ID, :PAY-GROSS,
                           CURRENT TIMESTAMP)
               END-EXEC
           ELSE
               MOVE 'P' TO PAY-STATUS
           END-IF.
           WRITE PAY-RECORD.
