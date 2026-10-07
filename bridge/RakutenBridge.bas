Attribute VB_Name = "RakutenBridge"
' =====================================================================
' toshi <-> 楽天証券 マーケットスピードII RSS ブリッジ (VBA 雛形)
'
' 使い方: マーケットスピードII を起動しログイン → RSS 付き Excel を開く →
'         このモジュールをインポート → StartBridge を実行。
' 通信: BRIDGE_DIR 配下のテキストファイル (key=value の行)
'   orders\<id>.txt  Python→Excel  id / symbol / side(buy|sell) / qty / type(market)
'   fills\<id>.txt   Excel→Python  status(filled|rejected) / price / message
'   state.txt        Excel→Python  updated / cash / pos=銘柄,株数,平均取得単価 (複数行)
'
' !! 重要 !! 下の3つの *ViaRss 関数は未実装のスタブです。
'    楽天証券「マーケットスピードII RSS」公式マニュアルの発注・余力・建玉の関数を
'    使って実装してください。実装するまで live モードでは発注されません(全て rejected)。
'    実装後は必ず少額・1銘柄・手動監視で検証してください。
' =====================================================================
Option Explicit

Private Const BRIDGE_DIR As String = "C:\toshi\bridge"   ' Python 側 TOSHI_BRIDGE_DIR と同じ場所
Private Const TICK_SEC As Long = 2
Private running As Boolean

Public Sub StartBridge()
    running = True
    Tick
End Sub

Public Sub StopBridge()
    running = False
End Sub

Private Sub Tick()
    If Not running Then Exit Sub
    On Error Resume Next
    WriteState
    ProcessOrders
    On Error GoTo 0
    Application.OnTime Now + TimeSerial(0, 0, TICK_SEC), "Tick"
End Sub

Private Sub WriteState()
    Dim f As Integer, tmp As String, lines As Variant, i As Long
    tmp = BRIDGE_DIR & "\state.tmp"
    f = FreeFile
    Open tmp For Output As #f
    Print #f, "updated=" & Format(Now, "yyyy-mm-dd hh:nn:ss")
    Print #f, "cash=" & CStr(GetCashViaRss())
    lines = GetPositionsViaRss()          ' 各要素 "銘柄,株数,平均取得単価"
    If IsArray(lines) Then
        For i = LBound(lines) To UBound(lines)
            Print #f, "pos=" & lines(i)
        Next
    End If
    Close #f
    If Dir(BRIDGE_DIR & "\state.txt") <> "" Then Kill BRIDGE_DIR & "\state.txt"
    Name tmp As BRIDGE_DIR & "\state.txt"
End Sub

Private Sub ProcessOrders()
    Dim fn As String, kv As Object, status As String, price As Double, msg As String
    fn = Dir(BRIDGE_DIR & "\orders\*.txt")
    Do While fn <> ""
        Set kv = ReadKv(BRIDGE_DIR & "\orders\" & fn)
        PlaceOrderViaRss kv("symbol"), kv("side"), CLng(kv("qty")), status, price, msg
        WriteFill kv("id"), status, price, msg
        Name BRIDGE_DIR & "\orders\" & fn As BRIDGE_DIR & "\orders\" & fn & ".done"
        fn = Dir
    Loop
End Sub

Private Function ReadKv(path As String) As Object
    Dim d As Object, f As Integer, ln As String, p As Long
    Set d = CreateObject("Scripting.Dictionary")
    f = FreeFile
    Open path For Input As #f
    Do While Not EOF(f)
        Line Input #f, ln
        p = InStr(ln, "=")
        If p > 0 Then d(Left$(ln, p - 1)) = Mid$(ln, p + 1)
    Loop
    Close #f
    Set ReadKv = d
End Function

Private Sub WriteFill(id As String, status As String, price As Double, msg As String)
    Dim f As Integer
    f = FreeFile
    Open BRIDGE_DIR & "\fills\" & id & ".txt" For Output As #f
    Print #f, "status=" & status
    Print #f, "price=" & CStr(price)
    Print #f, "message=" & msg
    Close #f
End Sub

' ---------------- ここから未実装スタブ (要実装) ----------------
Private Sub PlaceOrderViaRss(symbol As String, side As String, qty As Long, _
                             ByRef status As String, ByRef price As Double, ByRef msg As String)
    ' TODO: RSS の発注関数で 現物・成行・当日限り の注文を出し、約定を確認して
    '       status="filled", price=約定単価 をセットする。失敗時は status="rejected" と msg。
    status = "rejected": price = 0
    msg = "PlaceOrderViaRss 未実装"
End Sub

Private Function GetCashViaRss() As Double
    ' TODO: RSS の余力(買付可能額)取得関数で実装
    GetCashViaRss = 0
End Function

Private Function GetPositionsViaRss() As Variant
    ' TODO: RSS の建玉・保有株取得関数で実装。戻り値は "銘柄,株数,平均取得単価" の配列
    GetPositionsViaRss = Array()
End Function
