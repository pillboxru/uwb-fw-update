"""Файл прошивки .wbfw: 32-байтный информационный блок + данные, записываются по 68 регистров."""

import struct

INFO_BLOCK_REGS = 16
DATA_BLOCK_REGS = 68


class WbfwError(Exception):
    pass


class Wbfw:
    def __init__(self, data: bytes, name: str = ""):
        if len(data) % 2:
            raise WbfwError(f"{name}: длина файла прошивки должна быть чётной ({len(data)} байт)")
        if len(data) <= INFO_BLOCK_REGS * 2:
            raise WbfwError(f"{name}: файл слишком короткий ({len(data)} байт)")
        regs = list(struct.unpack(f">{len(data) // 2}H", data))
        self.name = name
        self.info = regs[:INFO_BLOCK_REGS]
        payload = regs[INFO_BLOCK_REGS:]
        self.chunks = [payload[i:i + DATA_BLOCK_REGS] for i in range(0, len(payload), DATA_BLOCK_REGS)]

    @classmethod
    def load(cls, path):
        with open(path, "rb") as fp:
            return cls(fp.read(), str(path))

    def __len__(self):
        return len(self.chunks)
