import itertools

import torch
import torch.nn.functional as F
from pytorch_lightning import LightningModule

from meldataset import mel_spectrogram
from models.generator import Generator
from models.CoMBD import CoMBD
from models.SBD import SBD
from models.losses import feature_loss
from models.losses import generator_loss
from models.losses import discriminator_loss
from pqmf import PQMF


class Avocodo(LightningModule):
    def __init__(
        self,
        h
    ):
        super().__init__()
        self.save_hyperparameters(h)

        self.pqmf_lv2 = PQMF(*self.hparams.pqmf_config["lv2"])
        self.pqmf_lv1 = PQMF(*self.hparams.pqmf_config["lv1"])

        self.generator = Generator(self.hparams.generator)
        self.combd = CoMBD(self.hparams.combd, [self.pqmf_lv2, self.pqmf_lv1])
        self.sbd = SBD(self.hparams.sbd)
        self.automatic_optimization = False

    def configure_optimizers(self):
        h = self.hparams.optimizer
        opt_g = torch.optim.AdamW(self.generator.parameters(
        ), h.learning_rate, betas=[h.adam_b1, h.adam_b2])
        opt_d = torch.optim.AdamW(itertools.chain(self.combd.parameters(), self.sbd.parameters()),
                                  h.learning_rate, betas=[h.adam_b1, h.adam_b2])
        return [opt_g, opt_d], []

    def forward(self, z):
        return self.generator(z)[-1]

    def training_step(self, batch, batch_idx):
        x, y, _, y_mel = batch
        y = y.unsqueeze(1)
        ys = [
            self.pqmf_lv2.analysis(
                y
            )[:, :self.hparams.generator.projection_filters[1]],
            self.pqmf_lv1.analysis(
                y
            )[:, :self.hparams.generator.projection_filters[2]],
            y
        ]

        y_g_hats = self.generator(x)

        # train generator
        y_du_hat_r, y_du_hat_g, fmap_u_r, fmap_u_g = self.combd(
            ys, y_g_hats)
        loss_fm_u, losses_fm_u = feature_loss(fmap_u_r, fmap_u_g)
        loss_gen_u, losses_gen_u = generator_loss(y_du_hat_g)

        y_ds_hat_r, y_ds_hat_g, fmap_s_r, fmap_s_g = self.sbd(
            y, y_g_hats[-1])
        loss_fm_s, losses_fm_s = feature_loss(fmap_s_r, fmap_s_g)
        loss_gen_s, losses_gen_s = generator_loss(y_ds_hat_g)

        # L1 Mel-Spectrogram Loss
        y_g_hat_mel = mel_spectrogram(
            y_g_hats[-1].squeeze(1),
            self.hparams.audio.n_fft,
            self.hparams.audio.num_mels,
            self.hparams.audio.sampling_rate,
            self.hparams.audio.hop_size,
            self.hparams.audio.win_size,
            self.hparams.audio.fmin,
            self.hparams.audio.fmax_for_loss
        )
        loss_mel = F.l1_loss(y_mel, y_g_hat_mel)
        self.log("train/l1_loss", loss_mel, prog_bar=True)
        loss_mel = loss_mel * self.hparams.loss_scale_mel

        g_loss = loss_gen_s + loss_gen_u + loss_fm_s + loss_fm_u + loss_mel

        self.log("train/g_loss", g_loss, prog_bar=True)

        # Manually perform optimization for the generator
        opt_gen = self.optimizers()[0]
        opt_gen.zero_grad()
        g_loss.backward()
        opt_gen.step()

        # train discriminator
        detached_y_g_hats = [x.detach() for x in y_g_hats]

        y_du_hat_r, y_du_hat_g, _, _ = self.combd(
            ys, detached_y_g_hats)
        loss_disc_u, losses_disc_u_r, losses_disc_u_g = discriminator_loss(
            y_du_hat_r, y_du_hat_g)

        y_ds_hat_r, y_ds_hat_g, _, _ = self.sbd(y, detached_y_g_hats[-1])
        loss_disc_s, losses_disc_s_r, losses_disc_s_g = discriminator_loss(
            y_ds_hat_r, y_ds_hat_g)

        d_loss = loss_disc_s + loss_disc_u
        self.log("train/d_loss", d_loss, prog_bar=True)

        # Manually perform optimization for the discriminator
        opt_disc = self.optimizers()[1]
        opt_disc.zero_grad()
        d_loss.backward()
        opt_disc.step()

        # Return a dictionary with 'loss' key
        return {'loss': g_loss + d_loss}


    def validation_step(self, batch, batch_idx):
        x, y, _, y_mel = batch
        y_g_hat = self(x)
        y_g_hat_mel = mel_spectrogram(
            y_g_hat.squeeze(1),
            self.hparams.audio.n_fft,
            self.hparams.audio.num_mels,
            self.hparams.audio.sampling_rate,
            self.hparams.audio.hop_size,
            self.hparams.audio.win_size,
            self.hparams.audio.fmin,
            self.hparams.audio.fmax_for_loss
        )
        val_loss = F.l1_loss(y_mel, y_g_hat_mel)
        self.logger.experiment.add_audio(
            f'pred/{batch_idx}', y_g_hat.squeeze(), self.current_epoch, self.hparams.audio.sampling_rate)
        self.logger.experiment.add_audio(
            f'gt/{batch_idx}', y[0].squeeze(), self.current_epoch, self.hparams.audio.sampling_rate)
        self.log("validation_step_outputs", val_loss)
        return val_loss

    def on_validation_epoch_end(self):
        # Access validation outputs from callback_metrics
        validation_step_outputs = self.trainer.callback_metrics.get("validation_step_outputs", [])

        # Check if validation_step_outputs is not empty
        if validation_step_outputs:
            # If there's only one tensor, convert it to a tuple
            if not isinstance(validation_step_outputs, tuple):
                validation_step_outputs = (validation_step_outputs,)

            # Perform your validation logic here using validation_step_outputs
            val_loss = torch.mean(torch.stack(validation_step_outputs))

            # Log the validation loss
            self.log("validation/l1_loss", val_loss, prog_bar=False)
        else:
            print("No validation step outputs logged during this epoch.")

