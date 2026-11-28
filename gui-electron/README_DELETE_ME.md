# 削除残渣

Host GUI の Electron 版は `gui-tauri` へ移行済みです。

このフォルダに残っている `node_modules/electron/.../default_app.asar` は Cursor がファイルロックしているため削除できませんでした。

Cursor を一度終了するか、該当ワークスペースから外したあと**、次を実行してください。

```powershell
Remove-Item -LiteralPath "c:\Users\coffi\cursor\ProjectMOMOKA\gui-electron" -Recurse -Force
```
