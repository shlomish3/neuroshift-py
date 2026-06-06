Attribute VB_Name = "Module2"
Option Explicit

' Month sheet layout:
'   each weekly block starts with 2 header rows,
'   then the 31 SHIFT_ORDER rows from core/export/excel.py,
'   then one "unassigned" formula row and one spacer row.
Private Const COL_FIRST As Long = 2
Private Const COL_LAST As Long = 8
Private Const BLOCK_FIRST_ROW As Long = 1
Private Const BLOCK_HEIGHT As Long = 35
Private Const BLOCK_DATA_OFFSET As Long = 2
Private Const SHIFT_COUNT As Long = 31
Private Const USE_CASE_SENSITIVE As Boolean = False

Private Const COLOR_DUP As Long = vbRed
Private Const COLOR_WARN As Long = 33023
Private Const COLOR_NORM As Long = vbBlack

Public Sub Auto_Open()
    ' Intentionally left blank.
    ' Automatic recoloring changes workbook formatting on open and clears Excel Undo.
    ' Duplicate highlighting is now generated as workbook conditional formatting.
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
    If lastRow < BLOCK_FIRST_ROW + BLOCK_DATA_OFFSET Then Exit Sub

    Dim oldEvents As Boolean, oldScreenUpdating As Boolean
    If manageApplicationState Then
        oldEvents = Application.EnableEvents
        oldScreenUpdating = Application.ScreenUpdating
        Application.ScreenUpdating = False
        Application.EnableEvents = False
        ws.Calculate
    End If

    On Error GoTo CleanUp

    ws.Range(ws.Cells(1, COL_FIRST), ws.Cells(lastRow, COL_LAST)).Font.Color = COLOR_NORM

    Dim blockStart As Long, colIdx As Long
    For blockStart = BLOCK_FIRST_ROW To lastRow Step BLOCK_HEIGHT
        If blockStart + BLOCK_DATA_OFFSET + SHIFT_COUNT - 1 <= lastRow Then
            For colIdx = COL_FIRST To COL_LAST
                ColorConflictsInBlock ws, blockStart, colIdx
            Next colIdx
        End If
    Next blockStart

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

Private Sub ColorConflictsInBlock(ByVal ws As Worksheet, ByVal blockStart As Long, ByVal colIdx As Long)
    Dim morningRows As Collection: Set morningRows = RowsForShiftIndexes(blockStart, MorningShiftIndexes())
    Dim nightRows As Collection: Set nightRows = RowsForShiftIndexes(blockStart, NightShiftIndexes())
    Dim offRows As Collection: Set offRows = RowsForShiftIndexes(blockStart, OffWarningShiftIndexes())

    Dim morningCounts As Object: Set morningCounts = NewDictionary()
    Dim nightCounts As Object: Set nightCounts = NewDictionary()
    Dim offCounts As Object: Set offCounts = NewDictionary()

    CountTokensInRows ws, colIdx, morningRows, morningCounts
    CountTokensInRows ws, colIdx, nightRows, nightCounts
    CountTokensInRows ws, colIdx, offRows, offCounts

    Dim morningDuplicates As Object: Set morningDuplicates = KeysWithMinimumCount(morningCounts, 2)
    Dim nightWarnings As Object: Set nightWarnings = KeysWithMinimumCount(nightCounts, 2)
    Dim offNightWarnings As Object: Set offNightWarnings = IntersectKeys(nightCounts, offCounts)

    AddKeys nightWarnings, offNightWarnings

    ColorRowsByKeys ws, colIdx, morningRows, morningDuplicates, COLOR_DUP
    ColorRowsByKeys ws, colIdx, nightRows, nightWarnings, COLOR_WARN
    ColorRowsByKeys ws, colIdx, offRows, offNightWarnings, COLOR_WARN
End Sub

Private Function MorningShiftIndexes() As Variant
    ' Zero-based indexes inside SHIFT_ORDER.
    ' Excludes: hospitalization day, night shifts, intubation.
    MorningShiftIndexes = Array( _
        0, 1, 2, 8, 9, _
        10, 11, 12, 13, 14, 15, 16, _
        17, 18, 19, 20, 21, 22, 23, _
        24, 25, 26, 27, 28, 29, 30 _
    )
End Function

Private Function NightShiftIndexes() As Variant
    NightShiftIndexes = Array(4, 5, 6)
End Function

Private Function OffWarningShiftIndexes() As Variant
    OffWarningShiftIndexes = Array(28, 29)
End Function

Private Function RowsForShiftIndexes(ByVal blockStart As Long, ByVal indexes As Variant) As Collection
    Dim rows As New Collection
    Dim i As Long
    For i = LBound(indexes) To UBound(indexes)
        rows.Add blockStart + BLOCK_DATA_OFFSET + CLng(indexes(i))
    Next i
    Set RowsForShiftIndexes = rows
End Function

Private Sub CountTokensInRows(ByVal ws As Worksheet, ByVal colIdx As Long, ByVal rows As Collection, ByVal counts As Object)
    Dim item As Variant, toks As Variant, tok As Variant, key As String
    For Each item In rows
        toks = SplitTokens(CStr(ws.Cells(CLng(item), colIdx).Value2))
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
    Dim txt As String: txt = CStr(target.Value2)
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

                On Error Resume Next
                target.Characters(charStart, charLen).Font.Color = colorValue
                On Error GoTo 0
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

Private Sub AddKeys(ByVal target As Object, ByVal source As Object)
    Dim key As Variant
    For Each key In source.Keys
        target(CStr(key)) = True
    Next key
End Sub

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
