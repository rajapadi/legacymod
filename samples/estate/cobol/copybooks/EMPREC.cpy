       01  EMP-RECORD.
           05  EMP-ID            PIC X(6).
           05  EMP-NAME          PIC X(30).
           05  EMP-HOURS         PIC 9(3)V99 COMP-3.
           05  EMP-RATE          PIC 9(4)V99 COMP-3.
           05  EMP-STATE         PIC XX.
           05  EMP-STATUS        PIC X.
               88  EMP-ACTIVE    VALUE 'A'.
               88  EMP-TERMED    VALUE 'T'.
