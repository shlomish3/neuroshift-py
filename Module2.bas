Attribute VB_Name = "Module2"
Option Explicit

'================ CONFIG ================
Private Const COL_FIRST As Long = 2   ' B
Private Const COL_LAST  As Long = 8   ' H
Private Const USE_CASE_SENSITIVE As Boolean = False

' Colors (font). To use fill instead, see notes below.
Private Const COLOR_DUP  As Long = vbRed
Private Const COLOR_NORM As Long = vbBlack
'=======================================

' Call this once to (re)color everything for a given sheet
Public Sub RecolorDuplicateNamesAllForSheet(ByVal ws As Worksheet)
    Dim blocks As Variant: blocks = RowBlocks()
    Dim c As Long, b As Long

    Application.ScreenUpdating = False
    Application.EnableEvents = False

    ' Reset all target areas first (so removed duplicates go back to normal)
    ResetAllTargetAreas ws, blocks

    ' Process each column and block
    For c = COL_FIRST To COL_LAST
        For b = LBound(blocks) To UBound(blocks)
            ColorBlockDuplicates ws, c, blocks(b)
        Next b
    Next c

    Application.EnableEvents = True
    Application.ScreenUpdating = True
End Sub

'---- Core: color duplicates for ONE column+block ----
Private Sub ColorBlockDuplicates(ByVal ws As Worksheet, ByVal colIdx As Long, ByVal blockDef As Variant)
    Dim top1 As Long, bot1 As Long, top2 As Long, bot2 As Long
    top1 = blockDef(0): bot1 = blockDef(1): top2 = blockDef(2): bot2 = blockDef(3)

    Dim counts As Object: Set counts = CreateObject("Scripting.Dictionary")
    Dim r As Long, toks As Variant, t As Variant, key As String

    ' 1) count tokens across both ranges (case-insensitive unless configured)
    For r = top1 To bot1
        toks = SplitTokens(CStr(ws.Cells(r, colIdx).Value2))
        For Each t In toks
            key = NormalizeKey(CStr(t))
            If Len(key) > 0 And key <> "-" Then counts(key) = counts(key) + 1
        Next t
    Next r
    For r = top2 To bot2
        toks = SplitTokens(CStr(ws.Cells(r, colIdx).Value2))
        For Each t In toks
            key = NormalizeKey(CStr(t))
            If Len(key) > 0 And key <> "-" Then counts(key) = counts(key) + 1
        Next t
    Next r

    ' 2) color only the duplicated token(s), not the entire cell
    For r = top1 To bot1
        ColorDuplicateTokensInCell ws.Cells(r, colIdx), counts
    Next r
    For r = top2 To bot2
        ColorDuplicateTokensInCell ws.Cells(r, colIdx), counts
    Next r
End Sub

'---- Helpers ----
Private Function RowBlocks() As Variant
    ' each item: Array(Top1, Bot1, Top2, Bot2)
    RowBlocks = Array( _
        Array(3, 5, 10, 30), _
        Array(35, 37, 42, 62), _
        Array(67, 69, 74, 94), _
        Array(99, 101, 106, 126), _
        Array(131, 133, 138, 158) _
    )
End Function

Private Sub ResetAllTargetAreas(ByVal ws As Worksheet, ByVal blocks As Variant)
    Dim b As Long, c As Long
    For c = COL_FIRST To COL_LAST
        For b = LBound(blocks) To UBound(blocks)
            ws.Range(ws.Cells(blocks(b)(0), c), ws.Cells(blocks(b)(1), c)).Font.Color = COLOR_NORM
            ws.Range(ws.Cells(blocks(b)(2), c), ws.Cells(blocks(b)(3), c)).Font.Color = COLOR_NORM
        Next b
    Next c
End Sub

Private Sub ColorDuplicateTokensInCell(ByVal target As Range, ByVal counts As Object)
    Dim txt As String: txt = CStr(target.Value2)
    If Len(txt) = 0 Then Exit Sub

    target.Font.Color = COLOR_NORM

    Dim raw As Variant: raw = Split(txt, ",")
    Dim i As Long, partText As String, tokenText As String, key As String
    Dim partStart As Long, firstOffset As Long, lastOffset As Long
    Dim charStart As Long, charLen As Long

    partStart = 1
    For i = LBound(raw) To UBound(raw)
        partText = CStr(raw(i))
        tokenText = Application.WorksheetFunction.Trim(Replace(partText, Chr$(160), " "))
        key = NormalizeKey(tokenText)

        If Len(key) > 0 And key <> "-" Then
            If counts.Exists(key) And CLng(counts(key)) >= 2 Then
                firstOffset = FirstNonSpaceOffset(partText)
                lastOffset = LastNonSpaceOffset(partText)

                If firstOffset > 0 And lastOffset >= firstOffset Then
                    charStart = partStart + firstOffset - 1
                    charLen = lastOffset - firstOffset + 1

                    On Error Resume Next
                    target.Characters(charStart, charLen).Font.Color = COLOR_DUP
                    On Error GoTo 0
                End If
            End If
        End If
        partStart = partStart + Len(partText) + 1   ' +1 for comma
    Next i
End Sub

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

' Split by comma, trim spaces, remove NBSPs and empties
Private Function SplitTokens(ByVal txt As String) As Variant
    Dim s As String: s = Replace(txt, Chr$(160), " ")                ' NBSP -> space
    s = Application.WorksheetFunction.Trim(s)                        ' collapse/trim
    Dim raw As Variant: raw = Split(s, ",")
    Dim out() As String, i As Long, n As Long, tok As String
    ReDim out(0 To 0): n = -1
    For i = LBound(raw) To UBound(raw)
        tok = Application.WorksheetFunction.Trim(CStr(raw(i)))
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

Private Function NormalizeKey(ByVal s As String) As String
    If USE_CASE_SENSITIVE Then NormalizeKey = s Else NormalizeKey = LCase$(s)
End Function




