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

    ' 2) color cells in both ranges that contain any duplicated token (count >= 2)
    For r = top1 To bot1
        toks = SplitTokens(CStr(ws.Cells(r, colIdx).Value2))
        If AnyDupToken(toks, counts) Then
            ws.Cells(r, colIdx).Font.Color = COLOR_DUP
        End If
    Next r
    For r = top2 To bot2
        toks = SplitTokens(CStr(ws.Cells(r, colIdx).Value2))
        If AnyDupToken(toks, counts) Then
            ws.Cells(r, colIdx).Font.Color = COLOR_DUP
        End If
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

Private Function AnyDupToken(ByVal tokens As Variant, ByVal counts As Object) As Boolean
    Dim t As Variant, k As String
    For Each t In tokens
        k = NormalizeKey(CStr(t))
        If Len(k) > 0 And k <> "-" Then
            If counts.Exists(k) Then
                If CLng(counts(k)) >= 2 Then AnyDupToken = True: Exit Function
            End If
        End If
    Next t
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




