       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYIMS.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  GU-FUNC               PIC X(4) VALUE 'GU  '.
       01  EMP-SEG-AREA          PIC X(44).
       LINKAGE SECTION.
       01  EMP-PCB.
           05  PCB-DBD-NAME      PIC X(8).
           05  PCB-SEG-LEVEL     PIC XX.
           05  PCB-STATUS        PIC XX.
       PROCEDURE DIVISION.
       ENTRY 'DLITCBL' USING EMP-PCB.
       MAIN-PARA.
           CALL 'CBLTDLI' USING GU-FUNC, EMP-PCB, EMP-SEG-AREA.
           IF PCB-STATUS NOT = SPACES
               DISPLAY 'IMS GU FAILED ' PCB-STATUS
           END-IF.
           GOBACK.
