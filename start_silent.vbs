Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "H:\code\twitter-tag-gallery"
WshShell.Run """C:\Users\Gary\AppData\Local\Programs\Python\Python310\pythonw.exe"" ""H:\code\twitter-tag-gallery\start_background.pyw""", 0, False
