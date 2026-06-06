Attribute VB_Name = "Module2"
Option Explicit

Private Const COL_FIRST As Long = 2
Private Const COL_LAST As Long = 8
Private Const USE_CASE_SENSITIVE As Boolean = False

Private Const COLOR_DUP As Long = vbRed
Private Const COLOR_WARN As Long = 33023
Private Const COLOR_NORM As Long = vbBlack

Public Sub Auto_Open()
    RecolorNeuroShiftWorkbook
End Sub

Public Sub RecolorNeuroShiftWorkbook()
    Dim oldEvents As Boolean: oldEvents = Application.EnableEvents
    Dim oldScreenUpdating As Boolean: oldScreenUpdating = Application.ScreenUpdating

    Application.ScreenUpdating = False
    Application.EnableEvents = False
    On Error GoTo CleanUp

    Application.CalculateFullRebuild

    Dim ws As Worksheet
    For Each ws In ThisWorkbook.Worksheets
        If IsMonthSheet(ws) Then RecolorDuplicateNamesAllForSheet ws, False
    Next ws

CleanUp:
    Application.EnableEvents = oldEvents
    Application.ScreenUpdating = oldScreenUpdating
End Sub

Public Sub RecolorDuplicateNamesAllForSheet(ByVal ws As Worksheet, Optional ByVal manageApplicationState As Boolean = True)
    Dim lastRow As Long: lastRow = LastUsedRow(ws)
    If lastRow < 3 Then Exit Sub

    Dim oldEvents As Boolean, oldScreenUpdating As Boolean
    If manageApplicationState Then
        oldEvents = Application.EnableEvents
        oldScreenUpdating = Application.ScreenUpdating
        Application.ScreenUpdating = False
        Application.EnableEvents = False
        ws.Calculate
    End If

    On Error GoTo CleanUp

    ' Reset font color to black for data area
    ws.Range(ws.Cells(1, COL_FIRST), ws.Cells(lastRow, COL_LAST)).Font.Color = COLOR_NORM

    Dim r As Long, colIdx As Long
    Dim blockStart As Long, dataRow As Long
    Dim shiftName As String
    
    Dim morningRows As Collection
    Dim nightRows As Collection
    Dim offRows As Collection

    r = 1
    Do While r <= lastRow
        If IsBlockHeader(SafeCStr(ws.Cells(r, 1).Value2)) Then
            blockStart = r
            dataRow = blockStart + 1
            
            Set morningRows = New Collection
            Set nightRows = New Collection
            Set offRows = New Collection
            
            Do While dataRow <= lastRow
                shiftName = SafeCStr(ws.Cells(dataRow, 1).Value2)
                If shiftName = "" Or IsUnassignedRow(shiftName) Then Exit Do
                
                If IsNightShift(shiftName) Then
                    nightRows.Add dataRow
                ElseIf IsOffWarningShift(shiftName) Then
                    offRows.Add dataRow
                    morningRows.Add dataRow
                ElseIf IsExcludedShift(shiftName) Then
                    ' Ignore excluded shifts
                Else
                    morningRows.Add dataRow
                End If
                dataRow = dataRow + 1
            Loop
            
            For colIdx = COL_FIRST To COL_LAST
                ColorConflictsInBlock ws, colIdx, morningRows, nightRows, offRows
            Next colIdx
            
            r = dataRow ' skip past this block
        Else
            r = r + 1
        End If
    Loop

CleanUp:
    If manageApplicationState Then
        Application.EnableEvents = oldEvents
        Application.ScreenUpdating = oldScreenUpdating
    End If
End Sub

Private Function IsMonthSheet(ByVal ws As Worksheet) As Boolean
    If Len(ws.Name) <> 7 Then Exit Function
    If Mid$(ws.Name, 5, 1) <> "-" Then Exit Function
    If Not IsNumeric(Left$(ws.Name, 4)) Then Exit Function
    If Not IsNumeric(Right$(ws.Name, 2)) Then Exit Function
    IsMonthSheet = True
End Function

Private Function IsBlockHeader(ByVal shiftName As String) As Boolean
    IsBlockHeader = (NormalizeKey(shiftName) = NormalizeKey(HebTask()))
End Function

Private Function IsUnassignedRow(ByVal shiftName As String) As Boolean
    IsUnassignedRow = (InStr(1, shiftName, HebUnassigned(), vbTextCompare) > 0)
End Function

Private Function IsNightShift(ByVal shiftName As String) As Boolean
    Dim key As String: key = NormalizeKey(shiftName)
    IsNightShift = (key = NormalizeKey(HebToranMion()) _
        Or key = NormalizeKey(HebToranMion2()) _
        Or key = NormalizeKey(HebConanMion()))
End Function

Private Function IsOffWarningShift(ByVal shiftName As String) As Boolean
    Dim key As String: key = NormalizeKey(shiftName)
    IsOffWarningShift = (key = NormalizeKey(HebVacation()) _
        Or key = NormalizeKey(HebAlternate()))
End Function

Private Function IsExcludedShift(ByVal shiftName As String) As Boolean
    Dim key As String: key = NormalizeKey(shiftName)
    IsExcludedShift = (key = NormalizeKey(HebDayAdmission()) _
        Or key = NormalizeKey(HebIntubation()))
End Function

' Keep Hebrew literals out of the .bas source. Excel imports .bas files using
' the local ANSI code page, which can corrupt UTF-8 Hebrew string literals.
Private Function HebTask() As String
    HebTask = ChrW$(&H05EA) & ChrW$(&H05E4) & ChrW$(&H05E7) & ChrW$(&H05D9) & ChrW$(&H05D3)
End Function

Private Function HebUnassigned() As String
    HebUnassigned = ChrW$(&H05DC) & ChrW$(&H05D0) & " " & ChrW$(&H05E9) & ChrW$(&H05D5) & ChrW$(&H05D1) & ChrW$(&H05E6) & ChrW$(&H05D5)
End Function

Private Function HebToranMion() As String
    HebToranMion = ChrW$(&H05EA) & "." & ChrW$(&H05DE) & ChrW$(&H05D9) & ChrW$(&H05D5) & ChrW$(&H05DF)
End Function

Private Function HebToranMion2() As String
    HebToranMion2 = HebToranMion() & " 2"
End Function

Private Function HebConanMion() As String
    HebConanMion = ChrW$(&H05DB) & ChrW$(&H05D5) & ChrW$(&H05E0) & ChrW$(&H05DF) & " " & ChrW$(&H05DE) & ChrW$(&H05D9) & ChrW$(&H05D5) & ChrW$(&H05DF)
End Function

Private Function HebVacation() As String
    HebVacation = ChrW$(&H05D7) & ChrW$(&H05D5) & ChrW$(&H05E4) & ChrW$(&H05E9)
End Function

Private Function HebAlternate() As String
    HebAlternate = ChrW$(&H05D7) & ChrW$(&H05DC) & ChrW$(&H05D5) & ChrW$(&H05E4) & ChrW$(&H05D9)
End Function

Private Function HebDayAdmission() As String
    HebDayAdmission = ChrW$(&H05D0) & ChrW$(&H05E9) & ChrW$(&H05E4) & ChrW$(&H05D5) & ChrW$(&H05D6) & " " & ChrW$(&H05D9) & ChrW$(&H05D5) & ChrW$(&H05DD)
End Function

Private Function HebIntubation() As String
    HebIntubation = ChrW$(&H05D0) & ChrW$(&H05D9) & ChrW$(&H05E0) & ChrW$(&H05D8) & ChrW$(&H05D5) & ChrW$(&H05D1) & ChrW$(&H05E6) & ChrW$(&H05D9) & ChrW$(&H05D4)
End Function

Private Sub ColorConflictsInBlock(ByVal ws As Worksheet, ByVal colIdx As Long, ByVal morningRows As Collection, ByVal nightRows As Collection, ByVal offRows As Collection)
    Dim morningCounts As Object: Set morningCounts = NewDictionary()
    Dim nightCounts As Object: Set nightCounts = NewDictionary()
    Dim offCounts As Object: Set offCounts = NewDictionary()

    CountTokensInRows ws, colIdx, morningRows, morningCounts
    CountTokensInRows ws, colIdx, nightRows, nightCounts
    CountTokensInRows ws, colIdx, offRows, offCounts

    Dim morningDuplicates As Object: Set morningDuplicates = KeysWithMinimumCount(morningCounts, 2)
    Dim nightDuplicates As Object: Set nightDuplicates = KeysWithMinimumCount(nightCounts, 2)
    Dim offNightWarnings As Object: Set offNightWarnings = IntersectKeys(nightCounts, offCounts)

    ColorRowsByKeys ws, colIdx, morningRows, morningDuplicates, COLOR_DUP
    ColorRowsByKeys ws, colIdx, nightRows, offNightWarnings, COLOR_WARN
    ColorRowsByKeys ws, colIdx, offRows, offNightWarnings, COLOR_WARN
    ColorRowsByKeys ws, colIdx, nightRows, nightDuplicates, COLOR_DUP
End Sub

Private Sub CountTokensInRows(ByVal ws As Worksheet, ByVal colIdx As Long, ByVal rows As Collection, ByVal counts As Object)
    Dim item As Variant, toks As Variant, tok As Variant, key As String
    For Each item In rows
        toks = SplitTokens(SafeCStr(ws.Cells(CLng(item), colIdx).Value2))
        For Each tok In toks
            key = NormalizeKey(CStr(tok))
            If Len(key) > 0 And key <> "-" Then IncrementCount counts, key
        Next tok
    Next item
End Sub

Private Sub ColorRowsByKeys(ByVal ws As Worksheet, ByVal colIdx As Long, ByVal rows As Collection, ByVal keys As Object, ByVal colorValue As Long)
    If keys.Count = 0 Then Exit Sub

    Dim item As Variant
    For Each item In rows
        ColorTokensInCell ws.Cells(CLng(item), colIdx), keys, colorValue
    Next item
End Sub

Private Sub ColorTokensInCell(ByVal target As Range, ByVal keys As Object, ByVal colorValue As Long)
    Dim txt As String: txt = SafeCStr(target.Value2)
    If Len(txt) = 0 Then Exit Sub

    Dim raw As Variant: raw = Split(txt, ",")
    Dim i As Long, partText As String, tokenText As String, key As String
    Dim partStart As Long, firstOffset As Long, lastOffset As Long
    Dim charStart As Long, charLen As Long

    partStart = 1
    For i = LBound(raw) To UBound(raw)
        partText = CStr(raw(i))
        tokenText = CleanToken(partText)
        key = NormalizeKey(tokenText)

        If Len(key) > 0 And key <> "-" And keys.Exists(key) Then
            firstOffset = TokenStartOffset(partText)
            lastOffset = LastNonSpaceOffset(partText)

            If firstOffset > 0 And lastOffset >= firstOffset Then
                charStart = partStart + firstOffset - 1
                charLen = lastOffset - firstOffset + 1

                
                If target.HasFormula Then
                    target.Font.Color = colorValue
                Else
                    On Error Resume Next
                    target.Characters(charStart, charLen).Font.Color = colorValue
                    On Error GoTo 0
                End If
                
            End If
        End If

        partStart = partStart + Len(partText) + 1
    Next i
End Sub

Private Function NewDictionary() As Object
    Dim d As Object: Set d = CreateObject("Scripting.Dictionary")
    If USE_CASE_SENSITIVE Then
        d.CompareMode = vbBinaryCompare
    Else
        d.CompareMode = vbTextCompare
    End If
    Set NewDictionary = d
End Function

Private Function KeysWithMinimumCount(ByVal counts As Object, ByVal minCount As Long) As Object
    Dim d As Object: Set d = NewDictionary()
    Dim key As Variant
    For Each key In counts.Keys
        If CLng(counts(key)) >= minCount Then d(CStr(key)) = True
    Next key
    Set KeysWithMinimumCount = d
End Function

Private Function IntersectKeys(ByVal leftCounts As Object, ByVal rightCounts As Object) As Object
    Dim d As Object: Set d = NewDictionary()
    Dim key As Variant
    For Each key In leftCounts.Keys
        If rightCounts.Exists(CStr(key)) Then d(CStr(key)) = True
    Next key
    Set IntersectKeys = d
End Function

Private Sub IncrementCount(ByVal counts As Object, ByVal key As String)
    If counts.Exists(key) Then
        counts(key) = CLng(counts(key)) + 1
    Else
        counts(key) = 1
    End If
End Sub

Private Function SplitTokens(ByVal txt As String) As Variant
    Dim s As String: s = CleanText(txt)
    Dim raw As Variant: raw = Split(s, ",")
    Dim out() As String, i As Long, n As Long, tok As String
    ReDim out(0 To 0): n = -1

    For i = LBound(raw) To UBound(raw)
        tok = CleanToken(CStr(raw(i)))
        If Len(tok) > 0 Then
            n = n + 1
            If n > UBound(out) Then ReDim Preserve out(0 To n)
            out(n) = tok
        End If
    Next i

    If n = -1 Then
        SplitTokens = Array()
    Else
        SplitTokens = out
    End If
End Function

Private Function CleanText(ByVal txt As String) As String
    CleanText = Application.WorksheetFunction.Trim(Replace(txt, Chr$(160), " "))
End Function

Private Function CleanToken(ByVal txt As String) As String
    Dim s As String: s = CleanText(txt)
    Dim startAt As Long: startAt = WarningNameStartOffset(s)
    If startAt > 1 Then
        CleanToken = CleanText(Mid$(s, startAt))
    Else
        CleanToken = s
    End If
End Function

Private Function NormalizeKey(ByVal s As String) As String
    If USE_CASE_SENSITIVE Then
        NormalizeKey = s
    Else
        NormalizeKey = LCase$(s)
    End If
End Function

Private Function LastUsedRow(ByVal ws As Worksheet) As Long
    Dim found As Range
    Set found = ws.Cells.Find(What:="*", LookIn:=xlFormulas, SearchOrder:=xlByRows, SearchDirection:=xlPrevious)
    If found Is Nothing Then
        LastUsedRow = 0
    Else
        LastUsedRow = found.Row
    End If
End Function

Private Function SafeCStr(ByVal val As Variant) As String
    If IsError(val) Then
        SafeCStr = ""
    ElseIf IsNull(val) Or IsEmpty(val) Then
        SafeCStr = ""
    Else
        SafeCStr = Trim(CStr(val))
    End If
End Function

Private Function FirstNonSpaceOffset(ByVal txt As String) As Long
    Dim i As Long, ch As String
    For i = 1 To Len(txt)
        ch = Mid$(txt, i, 1)
        If ch <> " " And ch <> Chr$(160) And ch <> vbTab Then
            FirstNonSpaceOffset = i
            Exit Function
        End If
    Next i
End Function

Private Function TokenStartOffset(ByVal txt As String) As Long
    Dim cleanStart As Long: cleanStart = FirstNonSpaceOffset(txt)
    If cleanStart = 0 Then Exit Function

    Dim trimmedText As String: trimmedText = CleanText(txt)
    Dim warningStart As Long: warningStart = WarningNameStartOffset(trimmedText)
    If warningStart <= 1 Then
        TokenStartOffset = cleanStart
    Else
        TokenStartOffset = cleanStart + warningStart - 1
    End If
End Function

Private Function WarningNameStartOffset(ByVal txt As String) As Long
    Dim firstSpace As Long, secondSpace As Long
    firstSpace = InStr(1, txt, " ")
    If firstSpace = 0 Then
        WarningNameStartOffset = 1
        Exit Function
    End If

    secondSpace = InStr(firstSpace + 1, txt, " ")
    If secondSpace = 0 Then
        WarningNameStartOffset = 1
        Exit Function
    End If

    If InStr(1, Left$(txt, secondSpace - 1), "/") > 0 Then
        WarningNameStartOffset = secondSpace + 1
    Else
        WarningNameStartOffset = 1
    End If
End Function

Private Function LastNonSpaceOffset(ByVal txt As String) As Long
    Dim i As Long, ch As String
    For i = Len(txt) To 1 Step -1
        ch = Mid$(txt, i, 1)
        If ch <> " " And ch <> Chr$(160) And ch <> vbTab Then
            LastNonSpaceOffset = i
            Exit Function
        End If
    Next i
End Function
