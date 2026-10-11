"""
Лаунчер: `python run.py`.

Запускает приложение без установки пакета,
если папка `index_checker/` лежит рядом с этим файлом.
"""

from index_checker.app import App


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()