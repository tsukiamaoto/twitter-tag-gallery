# Windows Common Item Dialog in folder-picking mode. Compatible with Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace TagGallery
{
    public static class ModernFolderPicker
    {
        // IFileDialog vtable order includes the inherited IModalWindow.Show slot.
        [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IFileDialog
        {
            [PreserveSig] int Show(IntPtr owner);
            void SetFileTypes(uint count, IntPtr filters);
            void SetFileTypeIndex(uint index);
            void GetFileTypeIndex(out uint index);
            void Advise(IntPtr events, out uint cookie);
            void Unadvise(uint cookie);
            void SetOptions(uint options);
            void GetOptions(out uint options);
            void SetDefaultFolder(IShellItem folder);
            void SetFolder(IShellItem folder);
            void GetFolder(out IShellItem folder);
            void GetCurrentSelection(out IShellItem item);
            void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
            void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
            void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
            void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
            void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
            void GetResult(out IShellItem item);
            void AddPlace(IShellItem item, uint placement);
            void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
            void Close(int result);
            void SetClientGuid(ref Guid guid);
            void ClearClientData();
            void SetFilter(IntPtr filter);
        }

        [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IShellItem
        {
            void BindToHandler(IntPtr context, ref Guid handler, ref Guid iid, out IntPtr result);
            void GetParent(out IShellItem parent);
            void GetDisplayName(uint type, [MarshalAs(UnmanagedType.LPWStr)] out string name);
            void GetAttributes(uint mask, out uint attributes);
            void Compare(IShellItem other, uint hint, out int order);
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int SHCreateItemFromParsingName(string path, IntPtr context, ref Guid iid, out IShellItem item);

        public static string Select(string initialPath, string title, string buttonText)
        {
            Application.EnableVisualStyles();
            IFileDialog dialog = null;
            IShellItem initial = null;
            IShellItem selected = null;
            using (Form owner = new Form())
            {
                owner.ShowInTaskbar = false;
                owner.TopMost = true;
                owner.StartPosition = FormStartPosition.CenterScreen;
                owner.Width = 1;
                owner.Height = 1;
                owner.Opacity = 0;
                try
                {
                    dialog = (IFileDialog)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")));
                    uint options;
                    dialog.GetOptions(out options);
                    // PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST | NOCHANGEDIR | DONTADDTORECENT
                    dialog.SetOptions(options | 0x20u | 0x40u | 0x800u | 0x8u | 0x02000000u);
                    dialog.SetTitle(title);
                    dialog.SetOkButtonLabel(buttonText);
                    if (!String.IsNullOrWhiteSpace(initialPath) && Directory.Exists(initialPath))
                    {
                        Guid iid = typeof(IShellItem).GUID;
                        int folderResult = SHCreateItemFromParsingName(initialPath, IntPtr.Zero, ref iid, out initial);
                        if (folderResult >= 0) dialog.SetFolder(initial);
                    }
                    owner.Show();
                    owner.Activate();
                    int result = dialog.Show(owner.Handle);
                    if (result == unchecked((int)0x800704c7)) return ""; // User cancelled.
                    Marshal.ThrowExceptionForHR(result);
                    dialog.GetResult(out selected);
                    string path;
                    selected.GetDisplayName(0x80058000u, out path); // SIGDN_FILESYSPATH
                    return path;
                }
                finally
                {
                    if (selected != null) Marshal.ReleaseComObject(selected);
                    if (initial != null) Marshal.ReleaseComObject(initial);
                    if (dialog != null) Marshal.ReleaseComObject(dialog);
                }
            }
        }
    }
}
'@

try {
    $selected = [TagGallery.ModernFolderPicker]::Select(
        $env:TAG_GALLERY_PICKER_INITIAL,
        $env:TAG_GALLERY_PICKER_TITLE,
        $env:TAG_GALLERY_PICKER_OK
    )
    @{ selected_path = $selected } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
