       IDENTIFICATION DIVISION.
       PROGRAM-ID. ORPHAN.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-HOURS              PIC 9(3)V99.
       01  WS-RATE               PIC 9(4)V99.
       01  WS-GROSS              PIC S9(7)V99 COMP-3.
       01  WS-AUDIT-AREA         PIC X(80).
       PROCEDURE DIVISION.
       MAIN-PARA.
           DISPLAY 'ORPHAN UTILITY - NOT REFERENCED ANYWHERE'.
           PERFORM CALC-COPY.
           CALL 'ASMXIT01' USING WS-AUDIT-AREA.
           GOBACK.
       CALC-COPY.
           IF WS-HOURS GREATER THAN 40
               COMPUTE WS-GROSS = (40 * WS-RATE)
                   + ((WS-HOURS - 40) * WS-RATE * 1.5)
           ELSE
               COMPUTE WS-GROSS = WS-HOURS * WS-RATE
           END-IF.
