       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYNOTIF.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-HCONN              PIC S9(9) BINARY.
       01  WS-COMPCODE           PIC S9(9) BINARY.
       01  WS-REASON             PIC S9(9) BINARY.
       01  MQOD.
           05  MQOD-OBJECTTYPE   PIC S9(9) BINARY VALUE 1.
           05  MQOD-OBJECTNAME   PIC X(48) VALUE 'BANK.ACK.QUEUE'.
       01  WS-MD                 PIC X(364).
       01  WS-PMO                PIC X(184).
       01  WS-MSG                PIC X(100).
       01  WS-MSGLEN             PIC S9(9) BINARY VALUE 100.
       PROCEDURE DIVISION.
       MAIN-PARA.
           MOVE 'PAYROLL CYCLE COMPLETE' TO WS-MSG.
           CALL 'MQPUT1' USING WS-HCONN, MQOD, WS-MD, WS-PMO,
               WS-MSGLEN, WS-MSG, WS-COMPCODE, WS-REASON.
           IF WS-COMPCODE NOT = 0
               DISPLAY 'MQPUT1 FAILED REASON ' WS-REASON
           END-IF.
           GOBACK.
