import os
import sys
import subprocess
import winreg

def get_shell_folder(folder_name: str) -> str:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders')
        val, _ = winreg.QueryValueEx(key, folder_name)
        return os.path.expandvars(val)
    except Exception:
        return os.path.join(os.environ['USERPROFILE'], folder_name)

desktop = get_shell_folder('Desktop')
startup = get_shell_folder('Startup')
work_dir = os.path.dirname(os.path.abspath(__file__))
pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
if not os.path.exists(pythonw):
    pythonw = sys.executable

script = os.path.join(work_dir, 'main.py')
icon = os.path.join(work_dir, 'app_icon.ico')

# Clean up old shortcuts if they exist
for old_loc in [desktop, work_dir, startup]:
    old_file = os.path.join(old_loc, "Smart Life RGB.lnk")
    if os.path.exists(old_file):
        try:
            os.remove(old_file)
        except Exception:
            pass

vbs_content = f'''
Set oWS = WScript.CreateObject("WScript.Shell")

' 1. Desktop
On Error Resume Next
Set oLink = oWS.CreateShortcut("{desktop}\\flowerRGB.lnk")
oLink.TargetPath = "{pythonw}"
oLink.Arguments = """{script}"""
oLink.WorkingDirectory = "{work_dir}"
oLink.IconLocation = "{icon},0"
oLink.Description = "flowerRGB — Управление подсветкой"
oLink.Save

' 2. Project Directory
Set oLink2 = oWS.CreateShortcut("{work_dir}\\flowerRGB.lnk")
oLink2.TargetPath = "{pythonw}"
oLink2.Arguments = """{script}"""
oLink2.WorkingDirectory = "{work_dir}"
oLink2.IconLocation = "{icon},0"
oLink2.Description = "flowerRGB — Управление подсветкой"
oLink2.Save

' 3. Windows Startup Folder
Set oLink3 = oWS.CreateShortcut("{startup}\\flowerRGB.lnk")
oLink3.TargetPath = "{pythonw}"
oLink3.Arguments = """{script}"""
oLink3.WorkingDirectory = "{work_dir}"
oLink3.IconLocation = "{icon},0"
oLink3.Description = "flowerRGB — Управление подсветкой"
oLink3.Save
'''

vbs_path = os.path.join(work_dir, '_make_shortcuts.vbs')
with open(vbs_path, 'w', encoding='utf-8') as f:
    f.write(vbs_content)

subprocess.run(['cscript', '//nologo', vbs_path], check=True)
if os.path.exists(vbs_path):
    os.remove(vbs_path)

print(f'Shortcuts created at:\n- Desktop: {desktop}\\flowerRGB.lnk\n- Project: {work_dir}\\flowerRGB.lnk\n- Startup: {startup}\\flowerRGB.lnk')
